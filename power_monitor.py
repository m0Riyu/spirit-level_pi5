"""INA219 power monitor for the Mcuzone 3003 21700 5V5A PD RP5 battery module.

Copied from the standalone project /home/user/power_test; settings in config.py. Three INA219 channels: charging,
battery discharge and 5 V output. A background thread reads them once a
second; snapshot() returns the newest values without touching the bus.

Channel addresses and shunt resistors follow the vendor script
INA219_10MR1126.py (shunt values still to be confirmed against a meter).
Everything marked 暫定 is a placeholder until measured, notably the voltage
thresholds and the discharge curve.

INA219 register facts used here (TI datasheet, fixed by the chip):
  0x01 shunt voltage: signed 16-bit, LSB 10 µV
  0x02 bus voltage:   bits 15..3, LSB 4 mV; bit 1 CNVR, bit 0 OVF
  0x03 power:         LSB = 20 × current LSB (depends on calibration 0x05)
  0x04 current:       signed, LSB = current LSB (depends on calibration 0x05)
This module only READS registers; it never writes configuration/calibration.
"""

import csv
import math
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from config import (  # noqa: E402  POWER_ settings live in config.py
    POWER_I2C_BUS,
    POWER_SAMPLE_INTERVAL_SECONDS,
    POWER_CHANNEL_ADDRESSES,
    POWER_SHUNT_OHMS,
    POWER_BATTERY_VOLTAGE_CHANNEL,
    POWER_SHUNT_PGA_MV,
    POWER_CHARGING_CURRENT_A,
    POWER_WARN_VOLTAGE_V,
    POWER_SHUTDOWN_VOLTAGE_V,
    POWER_HYSTERESIS_V,
    POWER_DEBOUNCE_SECONDS,
    POWER_THRESHOLDS_PROVISIONAL,
    POWER_CAPACITY_MAH,
    POWER_DISCHARGE_CURVE,
    POWER_DISCHARGE_CURVE_CALIBRATED,
    POWER_SOC_MAX_GAP_SECONDS,
    POWER_THROTTLED_COMMAND,
    POWER_THROTTLED_TIMEOUT_SECONDS,
)

# ============================================================== INA219 registers
INA219_CONFIG = 0x00
INA219_SHUNT_VOLTAGE = 0x01
INA219_BUS_VOLTAGE = 0x02
INA219_POWER = 0x03
INA219_CURRENT = 0x04
INA219_CALIBRATION = 0x05

BUS_VOLTAGE_LSB_V = 0.004
SHUNT_VOLTAGE_LSB_V = 10e-6
CHANNEL_NAMES = ("charge", "discharge", "output_5v")
SNAPSHOT_FIELDS = ("battery_v", "discharge_a", "output_5v_v", "output_5v_a", "charging", "throttled_hex", "status")


# ============================================================== pure conversions
def finite_or_none(value):
    """None for None/NaN/inf, else the float. Snapshots never carry NaN."""
    if value is None:
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def signed16(raw):
    if not 0 <= raw <= 0xFFFF:
        raise ValueError(f"not a 16-bit register value: {raw}")
    return raw - 0x10000 if raw & 0x8000 else raw


def swap16(word):
    """SMBus read_word_data is little-endian; INA219 sends MSB first."""
    return ((word & 0xFF) << 8) | ((word >> 8) & 0xFF)


@dataclass(frozen=True)
class BusVoltage:
    volts: float
    conversion_ready: bool
    overflow: bool  # math overflow: power/current registers are invalid


def bus_voltage(raw):
    return BusVoltage((raw >> 3) * BUS_VOLTAGE_LSB_V, bool(raw & 0x2), bool(raw & 0x1))


def shunt_voltage_v(raw):
    return signed16(raw) * SHUNT_VOLTAGE_LSB_V


def shunt_saturated(raw, pga_mv=POWER_SHUNT_PGA_MV):
    """At or beyond the PGA full scale the reading is clipped, not a real value."""
    return abs(signed16(raw)) >= round(pga_mv * 100)  # 10 µV per count


def current_from_shunt_a(shunt_v, shunt_ohm):
    shunt_v, shunt_ohm = finite_or_none(shunt_v), finite_or_none(shunt_ohm)
    if shunt_v is None or shunt_ohm is None or shunt_ohm <= 0:
        return None
    return shunt_v / shunt_ohm


def current_from_register_a(raw, current_lsb_a):
    current_lsb_a = finite_or_none(current_lsb_a)
    return None if not current_lsb_a else signed16(raw) * current_lsb_a


def power_from_register_w(raw, current_lsb_a):
    current_lsb_a = finite_or_none(current_lsb_a)
    return None if not current_lsb_a else raw * 20 * current_lsb_a


def power_w(volts, amps):
    volts, amps = finite_or_none(volts), finite_or_none(amps)
    return None if volts is None or amps is None else volts * amps


# ============================================================== channels / bus
@dataclass(frozen=True)
class ChannelConfig:
    name: str
    address: object = None       # 7-bit I2C address; None = 待廠商腳本確認
    shunt_ohm: object = None     # None = 待廠商腳本確認
    pga_mv: float = POWER_SHUNT_PGA_MV

    @property
    def configured(self):
        return self.address is not None and finite_or_none(self.shunt_ohm) not in (None, 0)


POWER_CHANNELS = {name: ChannelConfig(name, POWER_CHANNEL_ADDRESSES[name], POWER_SHUNT_OHMS[name])
                  for name in CHANNEL_NAMES}


@dataclass(frozen=True)
class ChannelReading:
    volts: object
    amps: object
    saturated: bool
    overflow: bool


def read_channel(bus, channel):
    bus_raw = bus.read_register(channel.address, INA219_BUS_VOLTAGE)
    shunt_raw = bus.read_register(channel.address, INA219_SHUNT_VOLTAGE)
    voltage = bus_voltage(bus_raw)
    saturated = shunt_saturated(shunt_raw, channel.pga_mv)
    amps = None if saturated else finite_or_none(current_from_shunt_a(shunt_voltage_v(shunt_raw), channel.shunt_ohm))
    # Round to the register resolution (bus 4 mV; shunt 10 µV ÷ shunt ≈ 1 mA at 10 mΩ).
    volts = finite_or_none(voltage.volts)
    return ChannelReading(None if volts is None else round(volts, 3), None if amps is None else round(amps, 4),
                          saturated, voltage.overflow)


class SMBusRegisterBus:
    """Real bus: big-endian 16-bit register reads over smbus2 (or smbus)."""

    def __init__(self, smbus):
        self._bus = smbus

    def read_register(self, address, register):
        return swap16(self._bus.read_word_data(address, register))

    def close(self):
        self._bus.close()


def open_smbus(bus_number=POWER_I2C_BUS):
    """Imported here only: unit tests and machines without smbus never need it."""
    try:
        from smbus2 import SMBus
    except ImportError:
        from smbus import SMBus
    return SMBusRegisterBus(SMBus(bus_number))


# ============================================================== get_throttled
THROTTLED_TEXT = {
    "under_voltage_now": "目前電壓不足", "throttled_now": "目前被限速",
    "under_voltage_occurred": "曾經電壓不足", "throttled_occurred": "曾經被限速",
}


@dataclass(frozen=True)
class ThrottledStatus:
    raw_hex: str
    under_voltage_now: bool       # bit 0  (0x1)
    throttled_now: bool           # bit 2  (0x4)
    under_voltage_occurred: bool  # bit 16 (0x10000)
    throttled_occurred: bool      # bit 18 (0x40000)

    def describe(self):
        flags = [text for name, text in THROTTLED_TEXT.items() if getattr(self, name)]
        return "、".join(flags) or "正常"


def parse_throttled(text):
    """'throttled=0x50005' -> ThrottledStatus; anything else -> None."""
    if not text or "=" not in text:
        return None
    try:
        value = int(text.strip().split("=", 1)[1], 16)
    except ValueError:
        return None
    return ThrottledStatus(hex(value), bool(value & 0x1), bool(value & 0x4),
                           bool(value & 0x10000), bool(value & 0x40000))


def read_throttled(run=subprocess.run):
    """Hex string such as '0x0', or None if vcgencmd is missing or fails."""
    try:
        result = run(list(POWER_THROTTLED_COMMAND), capture_output=True, text=True,
                     timeout=POWER_THROTTLED_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    status = parse_throttled(result.stdout)
    return status.raw_hex if status else None


# ============================================================== thresholds
@dataclass(frozen=True)
class ThresholdConfig:
    warn_v: float
    shutdown_v: float
    hysteresis_v: float
    debounce_seconds: float
    provisional: bool = True  # 暫定值：沒有實測依據

    def __post_init__(self):
        if not self.warn_v > self.shutdown_v:
            raise ValueError("warn_v must be above shutdown_v")
        if self.hysteresis_v < 0 or self.debounce_seconds < 0:
            raise ValueError("hysteresis and debounce must not be negative")


def default_threshold_config():
    return ThresholdConfig(POWER_WARN_VOLTAGE_V, POWER_SHUTDOWN_VOLTAGE_V, POWER_HYSTERESIS_V,
                           POWER_DEBOUNCE_SECONDS, POWER_THRESHOLDS_PROVISIONAL)


class BatteryAlarm:
    """normal -> warning -> shutdown with debounce and hysteresis.

    A level is entered only after the voltage stays at or below its threshold
    for debounce_seconds without a break. Warning clears only above
    warn_v + hysteresis_v. Shutdown is latched (a voltage rebound when the
    load drops must not cancel it); reset() clears it. A missing reading
    (None) neither starts, breaks nor completes a debounce.
    """

    def __init__(self, config=None, clock=time.monotonic):
        self.config = config or default_threshold_config()
        self.clock = clock
        self.reset()

    def reset(self):
        self.level = "normal"
        self._below_warn_since = None
        self._below_shutdown_since = None

    def update(self, volts):
        volts = finite_or_none(volts)
        if volts is None or self.level == "shutdown":
            return self.level
        now, config = self.clock(), self.config
        self._below_warn_since = (self._below_warn_since if self._below_warn_since is not None else now) \
            if volts <= config.warn_v else None
        self._below_shutdown_since = (self._below_shutdown_since if self._below_shutdown_since is not None else now) \
            if volts <= config.shutdown_v else None
        if self._below_shutdown_since is not None and now - self._below_shutdown_since >= config.debounce_seconds:
            self.level = "shutdown"
        elif self._below_warn_since is not None and now - self._below_warn_since >= config.debounce_seconds:
            self.level = "warning"
        elif self.level == "warning" and volts > config.warn_v + config.hysteresis_v:
            self.level = "normal"
        return self.level


# ============================================================== state of charge
def curve_percent(volts, curve):
    """Piecewise-linear lookup on ((volts, percent), ...); clamped at both ends."""
    volts = finite_or_none(volts)
    if volts is None or not curve:
        return None
    points = sorted(curve)
    if volts <= points[0][0]:
        return float(points[0][1])
    for (v0, p0), (v1, p1) in zip(points, points[1:]):
        if volts <= v1:
            return float(p0 + (p1 - p0) * (volts - v0) / (v1 - v0))
    return float(points[-1][1])


@dataclass(frozen=True)
class SocEstimate:
    percent: object       # None until a starting point is known
    consumed_mah: float
    calibrated: bool
    label: str            # "未校準" unless a measured curve is configured


class SocEstimator:
    """Discharge-curve start point + coulomb counting of the discharge current."""

    def __init__(self, capacity_mah=POWER_CAPACITY_MAH, curve=POWER_DISCHARGE_CURVE,
                 curve_calibrated=POWER_DISCHARGE_CURVE_CALIBRATED, clock=time.monotonic,
                 max_gap_seconds=POWER_SOC_MAX_GAP_SECONDS):
        self.capacity_mah = capacity_mah
        self.curve = curve
        self.calibrated = bool(curve) and bool(curve_calibrated)
        self.clock = clock
        self.max_gap_seconds = max_gap_seconds
        self.start_percent = None
        self.consumed_mah = 0.0
        self._last_time = None

    def update(self, volts, discharge_a):
        now = self.clock()
        if self.start_percent is None:
            self.start_percent = curve_percent(volts, self.curve)
        amps = finite_or_none(discharge_a)
        if self._last_time is not None and amps is not None and 0 < now - self._last_time <= self.max_gap_seconds:
            self.consumed_mah += amps * (now - self._last_time) / 3600 * 1000
        self._last_time = now
        percent = None
        if self.start_percent is not None:
            percent = min(100.0, max(0.0, self.start_percent - self.consumed_mah / self.capacity_mah * 100))
        return SocEstimate(percent, self.consumed_mah, self.calibrated, "" if self.calibrated else "未校準")


# ============================================================== monitor
class PowerMonitor:
    """Background reader; snapshot() never blocks on the bus or raises."""

    def __init__(self, bus_factory=open_smbus, *, channels=None, clock=time.monotonic,
                 throttled_reader=read_throttled, interval_seconds=POWER_SAMPLE_INTERVAL_SECONDS,
                 alarm=None, soc=None, battery_channel=POWER_BATTERY_VOLTAGE_CHANNEL,
                 charging_current_a=POWER_CHARGING_CURRENT_A):
        self.bus_factory = bus_factory
        self.channels = channels or POWER_CHANNELS
        self.clock = clock
        self.throttled_reader = throttled_reader
        self.interval_seconds = interval_seconds
        self.alarm = alarm or BatteryAlarm(clock=clock)
        self.soc = soc or SocEstimator(clock=clock)
        self.battery_channel = battery_channel
        self.charging_current_a = charging_current_a
        self._bus = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._snapshot = dict.fromkeys(SNAPSHOT_FIELDS)
        self._snapshot["status"] = "unavailable"
        self._details = {"error": "尚未讀取", "sampled_at": None, "alarm_level": self.alarm.level, "soc": None,
                         "discharge_w": None, "output_5v_w": None, "saturated": {}}

    # ---- public --------------------------------------------------------------
    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def snapshot(self):
        with self._lock:
            return dict(self._snapshot)

    def details(self):
        with self._lock:
            return dict(self._details)

    def start(self):
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="power-monitor", daemon=True)
        self._thread.start()

    def stop(self, timeout=5.0):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        self._close_bus()

    # ---- sampling ------------------------------------------------------------
    def _run(self):
        while not self._stop.is_set():
            self.sample_once()
            self._stop.wait(self.interval_seconds)

    def _close_bus(self):
        bus, self._bus = self._bus, None
        if bus is not None:
            try:
                bus.close()
            except Exception:  # closing a broken bus must not raise
                pass

    def sample_once(self):
        """Read every channel once. Errors become status 'unavailable', never exceptions."""
        readings, errors = {}, []
        missing = [name for name in CHANNEL_NAMES if not self.channels[name].configured]
        if missing:
            errors.append("通道未設定（待廠商腳本確認）：" + "、".join(missing))
        else:
            try:
                if self._bus is None:
                    self._bus = self.bus_factory()
            except Exception as error:
                errors.append(f"I2C 無法開啟：{type(error).__name__}: {error}")
            if self._bus is not None:
                for name in CHANNEL_NAMES:
                    try:
                        readings[name] = read_channel(self._bus, self.channels[name])
                    except Exception as error:
                        errors.append(f"{name} 讀取失敗：{type(error).__name__}: {error}")
        try:
            throttled = self.throttled_reader()
        except Exception:
            throttled = None
        if errors and self._bus is not None and len(readings) == 0:
            self._close_bus()  # reopen on the next sample

        battery = readings.get(self.battery_channel)
        discharge, output, charge = (readings.get(name) for name in ("discharge", "output_5v", "charge"))
        battery_v = battery.volts if battery else None
        discharge_a = discharge.amps if discharge else None
        snapshot = {
            "battery_v": battery_v,
            "discharge_a": discharge_a,
            "output_5v_v": output.volts if output else None,
            "output_5v_a": output.amps if output else None,
            "charging": None if charge is None or charge.amps is None else charge.amps > self.charging_current_a,
            "throttled_hex": throttled,
            "status": "unavailable" if errors else "ok",
        }
        alarm_level = self.alarm.update(battery_v)
        soc = self.soc.update(battery_v, discharge_a)
        details = {"error": "；".join(errors), "sampled_at": self.clock(), "alarm_level": alarm_level,
                   "soc": soc, "discharge_w": power_w(battery_v, discharge_a),
                   "output_5v_w": power_w(snapshot["output_5v_v"], snapshot["output_5v_a"]),
                   "saturated": {name: reading.saturated for name, reading in readings.items()}}
        with self._lock:
            self._snapshot, self._details = snapshot, details
        return snapshot


# ============================================================== CSV logging
class CsvLogger:
    """New CSV per run (never overwrites); every row is flushed and fsynced."""

    def __init__(self, directory, fields, stamp=None, prefix="power"):
        self.fields = list(fields)
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = stamp or datetime.now().strftime("%Y%m%d_%H%M%S")
        index = 0
        while True:
            path = directory / (f"{prefix}_{stamp}.csv" if index == 0 else f"{prefix}_{stamp}_{index}.csv")
            try:
                self._file = path.open("x", newline="", encoding="utf-8")
                break
            except FileExistsError:
                index += 1
        self.path = path
        self._writer = csv.DictWriter(self._file, self.fields)
        self._writer.writeheader()
        self._sync()

    def _sync(self):
        self._file.flush()
        os.fsync(self._file.fileno())

    def write(self, row):
        unknown = set(row) - set(self.fields)
        if unknown:
            raise ValueError("unknown CSV fields: " + ", ".join(sorted(unknown)))
        clean = {}
        for name in self.fields:
            value = row.get(name)
            if isinstance(value, float) and not math.isfinite(value):
                value = None
            clean[name] = "" if value is None else value
        self._writer.writerow(clean)
        self._sync()

    def close(self):
        if not self._file.closed:
            self._file.close()
