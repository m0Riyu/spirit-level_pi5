"""Unit tests for power_monitor.py: fake I2C bus and fake clock only, no hardware.

Run: python3 -m unittest -v
"""

import builtins
import csv
import math
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import power_monitor as pm


# ---------------------------------------------------------------- test doubles
class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeBus:
    """Register file per address; can be told to fail. Counts reads."""

    def __init__(self, registers=None):
        self.registers = registers or {}
        self.fail = False
        self.reads = 0
        self.lock = threading.Lock()

    def read_register(self, address, register):
        with self.lock:
            self.reads += 1
            if self.fail:
                raise OSError(121, "Remote I/O error")
            return self.registers[(address, register)]

    def close(self):
        pass


def bus_raw(volts):
    """Bus voltage register for `volts` (LSB 4 mV in bits 15..3, CNVR set)."""
    return (round(volts / 0.004) << 3) | 0x2


def shunt_raw(volts):
    """Shunt voltage register (signed, LSB 10 µV) as an unsigned 16-bit word."""
    return round(volts / 10e-6) & 0xFFFF


CHANNELS = {
    "charge": pm.ChannelConfig("charge", address=0x40, shunt_ohm=0.01),
    "discharge": pm.ChannelConfig("discharge", address=0x41, shunt_ohm=0.01),
    "output_5v": pm.ChannelConfig("output_5v", address=0x44, shunt_ohm=0.01),
}


def bus_with(battery_v=3.7, discharge_a=1.2, out_v=5.1, out_a=1.8, charge_a=0.0):
    return FakeBus({
        (0x41, pm.INA219_BUS_VOLTAGE): bus_raw(battery_v), (0x41, pm.INA219_SHUNT_VOLTAGE): shunt_raw(discharge_a * .01),
        (0x44, pm.INA219_BUS_VOLTAGE): bus_raw(out_v), (0x44, pm.INA219_SHUNT_VOLTAGE): shunt_raw(out_a * .01),
        (0x40, pm.INA219_BUS_VOLTAGE): bus_raw(5.0), (0x40, pm.INA219_SHUNT_VOLTAGE): shunt_raw(charge_a * .01),
    })


def monitor(bus, **kwargs):
    kwargs.setdefault("channels", CHANNELS)
    kwargs.setdefault("throttled_reader", lambda: "0x0")
    return pm.PowerMonitor(lambda: bus, **kwargs)


# ---------------------------------------------------------------- conversions
class ConversionTests(unittest.TestCase):
    def test_bus_voltage_uses_bits_15_to_3_at_4_mv(self):
        reading = pm.bus_voltage(bus_raw(3.712))
        self.assertAlmostEqual(reading.volts, 3.712, places=9)
        self.assertTrue(reading.conversion_ready)
        self.assertFalse(reading.overflow)

    def test_bus_voltage_zero_full_scale_and_flags(self):
        self.assertEqual(pm.bus_voltage(0).volts, 0.0)
        self.assertAlmostEqual(pm.bus_voltage(0xFFF8).volts, 8191 * .004, places=9)  # 32.764 V register maximum
        flags = pm.bus_voltage(0x0001)
        self.assertTrue(flags.overflow)
        self.assertFalse(flags.conversion_ready)

    def test_shunt_voltage_is_signed_10_microvolt(self):
        self.assertAlmostEqual(pm.shunt_voltage_v(0x0001), 10e-6, places=12)
        self.assertAlmostEqual(pm.shunt_voltage_v(0xFFFF), -10e-6, places=12)
        self.assertAlmostEqual(pm.shunt_voltage_v(0x7D00), .32, places=9)   # +32000 counts
        self.assertAlmostEqual(pm.shunt_voltage_v(0x8300), -.32, places=9)  # -32000 counts
        self.assertEqual(pm.shunt_voltage_v(0), 0.0)

    def test_current_from_shunt_handles_negative_zero_and_missing_resistor(self):
        self.assertAlmostEqual(pm.current_from_shunt_a(.012, .01), 1.2, places=9)
        self.assertAlmostEqual(pm.current_from_shunt_a(-.005, .01), -.5, places=9)
        self.assertEqual(pm.current_from_shunt_a(0., .01), 0.0)
        for bad in (None, 0., -.01, math.nan):
            self.assertIsNone(pm.current_from_shunt_a(.01, bad))

    def test_current_and_power_registers_scale_by_current_lsb(self):
        self.assertAlmostEqual(pm.current_from_register_a(1000, 1e-4), .1, places=12)
        self.assertAlmostEqual(pm.current_from_register_a(0xFC18, 1e-4), -.1, places=12)  # -1000 counts
        self.assertAlmostEqual(pm.power_from_register_w(500, 1e-4), 500 * 20 * 1e-4, places=12)
        self.assertEqual(pm.power_from_register_w(0, 1e-4), 0.0)
        self.assertIsNone(pm.current_from_register_a(1000, None))
        self.assertIsNone(pm.power_from_register_w(1000, 0))

    def test_shunt_saturation_at_pga_full_scale(self):
        self.assertTrue(pm.shunt_saturated(0x7D00, pga_mv=320))   # +320 mV
        self.assertTrue(pm.shunt_saturated(0x8300, pga_mv=320))   # -320 mV
        self.assertFalse(pm.shunt_saturated(0x7CFF, pga_mv=320))
        self.assertTrue(pm.shunt_saturated(0x0FA0, pga_mv=40))    # +40 mV at gain /1
        self.assertFalse(pm.shunt_saturated(0x0F9F, pga_mv=40))

    def test_signed16_boundaries(self):
        self.assertEqual(pm.signed16(0x7FFF), 32767)
        self.assertEqual(pm.signed16(0x8000), -32768)
        with self.assertRaises(ValueError):
            pm.signed16(0x10000)

    def test_byte_swap_for_smbus_words(self):
        self.assertEqual(pm.swap16(0x3412), 0x1234)
        self.assertEqual(pm.swap16(pm.swap16(0xBEEF)), 0xBEEF)

    def test_power_is_volts_times_amps_and_none_safe(self):
        self.assertAlmostEqual(pm.power_w(5.1, 1.8), 9.18, places=9)
        self.assertIsNone(pm.power_w(None, 1.))
        self.assertIsNone(pm.finite_or_none(math.nan))
        self.assertIsNone(pm.finite_or_none(math.inf))
        self.assertEqual(pm.finite_or_none(1.5), 1.5)


# ---------------------------------------------------------------- snapshot / thread
class SnapshotTests(unittest.TestCase):
    def test_snapshot_has_exactly_the_contract_fields(self):
        power = monitor(bus_with())
        power.sample_once()
        snapshot = power.snapshot()
        self.assertEqual(list(snapshot), ["battery_v", "discharge_a", "output_5v_v", "output_5v_a",
                                          "charging", "throttled_hex", "status"])
        self.assertEqual(snapshot["status"], "ok")
        self.assertAlmostEqual(snapshot["battery_v"], 3.7, places=2)
        self.assertAlmostEqual(snapshot["discharge_a"], 1.2, places=4)
        self.assertAlmostEqual(snapshot["output_5v_v"], 5.1, places=2)
        self.assertAlmostEqual(snapshot["output_5v_a"], 1.8, places=4)
        self.assertIs(snapshot["charging"], False)
        self.assertEqual(snapshot["throttled_hex"], "0x0")

    def test_before_first_read_everything_is_none_and_unavailable(self):
        snapshot = monitor(bus_with()).snapshot()
        self.assertEqual(snapshot["status"], "unavailable")
        self.assertTrue(all(value is None for key, value in snapshot.items() if key != "status"))

    def test_charging_follows_charge_channel_current(self):
        power = monitor(bus_with(charge_a=.8))
        power.sample_once()
        self.assertIs(power.snapshot()["charging"], True)

    def test_unconfigured_channels_are_unavailable_not_guessed(self):
        unknown = {name: pm.ChannelConfig(name, address=None, shunt_ohm=None) for name in pm.CHANNEL_NAMES}
        power = monitor(bus_with(), channels=unknown)
        power.sample_once()
        snapshot = power.snapshot()
        self.assertEqual(snapshot["status"], "unavailable")
        self.assertIsNone(snapshot["battery_v"])
        self.assertIn("未設定", power.details()["error"])

    def test_default_channels_follow_the_vendor_script(self):
        self.assertEqual({name: (channel.address, channel.shunt_ohm) for name, channel in pm.POWER_CHANNELS.items()},
                         {"charge": (0x40, .01), "discharge": (0x44, .01), "output_5v": (0x41, .01)})
        self.assertEqual(pm.POWER_BATTERY_VOLTAGE_CHANNEL, "discharge")
        self.assertTrue(all(channel.configured for channel in pm.POWER_CHANNELS.values()))

    def test_snapshot_never_contains_nan(self):
        bus = bus_with()
        bus.registers[(0x44, pm.INA219_SHUNT_VOLTAGE)] = shunt_raw(.01)
        power = monitor(bus, channels={**CHANNELS, "output_5v": pm.ChannelConfig("output_5v", 0x44, math.nan)})
        power.sample_once()
        self.assertIsNone(power.snapshot()["output_5v_a"])
        self.assertFalse(any(isinstance(value, float) and math.isnan(value) for value in power.snapshot().values()))


class ThreadTests(unittest.TestCase):
    def test_reads_every_interval_and_stops_reading_after_stop(self):
        bus = bus_with()
        power = monitor(bus, interval_seconds=.02)
        power.start()
        deadline = time.monotonic() + 2
        while bus.reads < 12 and time.monotonic() < deadline:
            time.sleep(.005)
        power.stop()
        self.assertGreaterEqual(bus.reads, 12)
        self.assertEqual(power.snapshot()["status"], "ok")
        count = bus.reads
        time.sleep(.1)
        self.assertEqual(bus.reads, count)
        self.assertFalse(power.running)

    def test_default_interval_is_one_second(self):
        self.assertEqual(pm.POWER_SAMPLE_INTERVAL_SECONDS, 1.0)
        self.assertEqual(monitor(bus_with()).interval_seconds, 1.0)

    def test_start_twice_and_stop_twice_are_safe(self):
        power = monitor(bus_with(), interval_seconds=.01)
        power.start()
        power.start()
        self.assertEqual(sum(thread.name == "power-monitor" for thread in threading.enumerate()), 1)
        power.stop()
        power.stop()

    def test_stop_returns_promptly_even_with_long_interval(self):
        power = monitor(bus_with(), interval_seconds=60)
        power.start()
        started = time.monotonic()
        power.stop()
        self.assertLess(time.monotonic() - started, 1)


# ---------------------------------------------------------------- errors
class ErrorTests(unittest.TestCase):
    def test_read_failure_is_unavailable_without_exception_then_recovers(self):
        bus = bus_with()
        power = monitor(bus)
        bus.fail = True
        power.sample_once()  # must not raise
        snapshot = power.snapshot()
        self.assertEqual(snapshot["status"], "unavailable")
        self.assertIsNone(snapshot["battery_v"])
        self.assertIn("OSError", power.details()["error"])
        bus.fail = False
        power.sample_once()
        self.assertEqual(power.snapshot()["status"], "ok")
        self.assertAlmostEqual(power.snapshot()["battery_v"], 3.7, places=2)

    def test_bus_that_cannot_open_is_retried_later(self):
        attempts = []

        def factory():
            attempts.append(1)
            if len(attempts) == 1:
                raise FileNotFoundError("/dev/i2c-1")
            return bus_with()

        power = pm.PowerMonitor(factory, channels=CHANNELS, throttled_reader=lambda: None)
        power.sample_once()
        self.assertEqual(power.snapshot()["status"], "unavailable")
        power.sample_once()
        self.assertEqual(power.snapshot()["status"], "ok")
        self.assertEqual(len(attempts), 2)

    def test_thread_survives_failures_and_snapshot_does_not_block(self):
        bus = bus_with()
        slow = threading.Event()
        original = bus.read_register

        def read(address, register):
            slow.wait(.5)  # a hung bus read
            return original(address, register)

        bus.read_register = read
        power = monitor(bus, interval_seconds=.01)
        power.start()
        started = time.monotonic()
        for _ in range(50):
            power.snapshot()
        self.assertLess(time.monotonic() - started, .2)  # never waits for the bus
        bus.fail = True
        slow.set()
        time.sleep(.05)
        self.assertTrue(power.running)
        self.assertEqual(power.snapshot()["status"], "unavailable")
        power.stop()

    def test_one_channel_failing_keeps_the_others(self):
        bus = bus_with()
        del bus.registers[(0x44, pm.INA219_BUS_VOLTAGE)]
        power = monitor(bus)
        power.sample_once()
        snapshot = power.snapshot()
        self.assertEqual(snapshot["status"], "unavailable")
        self.assertIsNone(snapshot["output_5v_v"])
        self.assertAlmostEqual(snapshot["battery_v"], 3.7, places=2)

    def test_hardware_module_is_imported_only_when_a_real_bus_is_built(self):
        real_import = builtins.__import__

        def no_smbus(name, *args, **kwargs):
            if name in ("smbus2", "smbus"):
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=no_smbus):
            power = pm.PowerMonitor(pm.open_smbus, channels=CHANNELS, throttled_reader=lambda: None)
            power.sample_once()
        self.assertEqual(power.snapshot()["status"], "unavailable")
        self.assertIn("smbus", power.details()["error"])


# ---------------------------------------------------------------- throttled
class ThrottledTests(unittest.TestCase):
    def test_bits(self):
        cases = {
            0x1: dict(under_voltage_now=True),
            0x4: dict(throttled_now=True),
            0x10000: dict(under_voltage_occurred=True),
            0x40000: dict(throttled_occurred=True),
        }
        for value, expected in cases.items():
            status = pm.parse_throttled(f"throttled={hex(value)}")
            self.assertEqual(status.raw_hex, hex(value))
            for field in ("under_voltage_now", "throttled_now", "under_voltage_occurred", "throttled_occurred"):
                self.assertEqual(getattr(status, field), expected.get(field, False), (value, field))

    def test_combined_and_zero(self):
        status = pm.parse_throttled("throttled=0x50005\n")
        self.assertTrue(status.under_voltage_now and status.throttled_now)
        self.assertTrue(status.under_voltage_occurred and status.throttled_occurred)
        self.assertFalse(any(vars(pm.parse_throttled("throttled=0x0")).get(name) for name in
                             ("under_voltage_now", "throttled_now", "under_voltage_occurred", "throttled_occurred")))
        self.assertIn("目前電壓不足", pm.parse_throttled("throttled=0x1").describe())

    def test_unparseable_output_is_none(self):
        for text in ("", "error", "throttled=zz", None):
            self.assertIsNone(pm.parse_throttled(text))

    def test_reader_handles_missing_command_failure_and_timeout(self):
        ok = subprocess.CompletedProcess([], 0, "throttled=0x50000\n", "")
        self.assertEqual(pm.read_throttled(run=lambda *a, **k: ok), "0x50000")
        failed = subprocess.CompletedProcess([], 255, "", "VCHI initialization failed")
        self.assertIsNone(pm.read_throttled(run=lambda *a, **k: failed))
        for error in (FileNotFoundError("vcgencmd"), subprocess.TimeoutExpired("vcgencmd", 2), PermissionError()):
            def run(*args, error=error, **kwargs):
                raise error
            self.assertIsNone(pm.read_throttled(run=run))

    def test_throttled_failure_does_not_make_power_unavailable(self):
        power = monitor(bus_with(), throttled_reader=lambda: None)
        power.sample_once()
        self.assertEqual(power.snapshot()["status"], "ok")
        self.assertIsNone(power.snapshot()["throttled_hex"])


# ---------------------------------------------------------------- thresholds
class ThresholdTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.config = pm.ThresholdConfig(warn_v=3.3, shutdown_v=3.1, hysteresis_v=.05, debounce_seconds=10)
        self.alarm = pm.BatteryAlarm(self.config, clock=self.clock)

    def feed(self, volts, seconds, step=1.):
        level = None
        for _ in range(int(round(seconds / step))):
            level = self.alarm.update(volts)
            self.clock.advance(step)
        return level

    def test_defaults_are_marked_provisional(self):
        self.assertEqual((pm.POWER_WARN_VOLTAGE_V, pm.POWER_SHUTDOWN_VOLTAGE_V), (3.3, 3.1))
        self.assertTrue(pm.POWER_THRESHOLDS_PROVISIONAL)
        self.assertTrue(pm.default_threshold_config().provisional)

    def test_warning_needs_n_continuous_seconds_below(self):
        self.assertEqual(self.feed(3.29, 9), "normal")
        self.assertEqual(self.feed(3.29, 2), "warning")

    def test_a_single_spike_above_restarts_the_debounce(self):
        self.feed(3.29, 9)
        self.alarm.update(3.40)  # above warn + hysteresis
        self.assertEqual(self.feed(3.29, 9), "normal")
        self.assertEqual(self.feed(3.29, 2), "warning")

    def test_hysteresis_prevents_toggling_near_the_threshold(self):
        self.feed(3.29, 11)
        for volts in (3.31, 3.29, 3.32, 3.30, 3.34):  # wobbling inside warn .. warn + hysteresis
            self.assertEqual(self.alarm.update(volts), "warning")
        self.assertEqual(self.alarm.update(3.36), "normal")  # above 3.35 clears

    def test_shutdown_after_debounce_and_latched(self):
        self.feed(3.2, 11)
        self.assertEqual(self.alarm.level, "warning")
        self.assertEqual(self.feed(3.09, 9), "warning")
        self.assertEqual(self.feed(3.09, 2), "shutdown")
        self.assertEqual(self.alarm.update(3.6), "shutdown")  # never cancelled by a rebound under load
        self.alarm.reset()
        self.assertEqual(self.alarm.level, "normal")

    def test_drop_straight_to_shutdown_level(self):
        self.assertEqual(self.feed(3.0, 11), "shutdown")

    def test_missing_reading_neither_triggers_nor_clears(self):
        self.feed(3.29, 5)
        self.alarm.update(None)
        self.clock.advance(10)
        self.assertEqual(self.alarm.update(None), "normal")
        self.assertEqual(self.alarm.update(3.29), "warning")  # 15 s continuously below, None in between

    def test_invalid_config_rejected(self):
        for bad in (dict(warn_v=3.1, shutdown_v=3.3), dict(hysteresis_v=-.1), dict(debounce_seconds=-1)):
            values = dict(warn_v=3.3, shutdown_v=3.1, hysteresis_v=.05, debounce_seconds=10)
            values.update(bad)
            with self.assertRaises(ValueError):
                pm.ThresholdConfig(**values)


# ---------------------------------------------------------------- state of charge
class SocTests(unittest.TestCase):
    CURVE = ((3.0, 0.), (3.4, 10.), (3.7, 50.), (4.1, 100.))

    def test_curve_interpolation_and_clamping(self):
        self.assertAlmostEqual(pm.curve_percent(3.55, self.CURVE), 30., places=9)
        self.assertEqual(pm.curve_percent(2.8, self.CURVE), 0.)
        self.assertEqual(pm.curve_percent(4.3, self.CURVE), 100.)
        self.assertIsNone(pm.curve_percent(None, self.CURVE))
        self.assertIsNone(pm.curve_percent(3.6, None))

    def test_without_curve_result_is_uncalibrated_but_counts_coulombs(self):
        clock = FakeClock()
        soc = pm.SocEstimator(capacity_mah=10000, curve=None, clock=clock, max_gap_seconds=3600)
        soc.update(3.7, 2.0)
        clock.advance(1800)
        estimate = soc.update(3.6, 2.0)
        self.assertIsNone(estimate.percent)
        self.assertFalse(estimate.calibrated)
        self.assertEqual(estimate.label, "未校準")
        self.assertAlmostEqual(estimate.consumed_mah, 1000., places=6)  # 2 A for 0.5 h

    def test_curve_start_then_coulomb_counting(self):
        clock = FakeClock()
        soc = pm.SocEstimator(capacity_mah=10000, curve=self.CURVE, curve_calibrated=True, clock=clock,
                              max_gap_seconds=7200)
        self.assertAlmostEqual(soc.update(3.7, 0.).percent, 50., places=9)
        clock.advance(3600)
        estimate = soc.update(3.66, 1.0)  # 1000 mAh = 10 % later
        self.assertAlmostEqual(estimate.percent, 40., places=6)
        self.assertTrue(estimate.calibrated)
        self.assertEqual(estimate.label, "")

    def test_generic_curve_is_still_uncalibrated(self):
        soc = pm.SocEstimator(capacity_mah=10000, curve=self.CURVE, curve_calibrated=False, clock=FakeClock())
        estimate = soc.update(3.7, 0.)
        self.assertEqual((estimate.percent, estimate.calibrated, estimate.label), (50., False, "未校準"))

    def test_data_gap_is_not_integrated(self):
        clock = FakeClock()
        soc = pm.SocEstimator(capacity_mah=10000, curve=None, clock=clock, max_gap_seconds=10)
        soc.update(3.7, 2.0)
        clock.advance(3600)  # an hour without readings
        self.assertEqual(soc.update(3.7, 2.0).consumed_mah, 0.)

    def test_missing_current_skips_integration_and_percent_is_clamped(self):
        clock = FakeClock()
        soc = pm.SocEstimator(capacity_mah=100, curve=self.CURVE, curve_calibrated=True, clock=clock)
        soc.update(3.4, None)
        clock.advance(5)
        self.assertEqual(soc.update(3.4, None).consumed_mah, 0.)
        for _ in range(100):
            clock.advance(5)
            estimate = soc.update(3.0, 50.)
        self.assertEqual(estimate.percent, 0.)


# ---------------------------------------------------------------- CSV logging
class LoggerTests(unittest.TestCase):
    def test_each_row_is_flushed_and_fsynced_and_files_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            fsyncs = []
            real_fsync = os.fsync
            with patch.object(pm.os, "fsync", side_effect=lambda fd: (fsyncs.append(fd), real_fsync(fd))):
                logger = pm.CsvLogger(directory, ["time", "battery_v"], stamp="20261007_220000")
                logger.write({"time": "t1", "battery_v": 3.7})
                logger.write({"time": "t2", "battery_v": None})
                first = logger.path
                second_logger = pm.CsvLogger(directory, ["time", "battery_v"], stamp="20261007_220000")
                logger.close()
                second_logger.close()
            self.assertEqual(len(fsyncs), 3 + 1)  # header + 2 rows, then the second file's header
            self.assertNotEqual(first, second_logger.path)
            self.assertEqual(first.name, "power_20261007_220000.csv")
            self.assertEqual(second_logger.path.name, "power_20261007_220000_1.csv")
            with first.open(newline="", encoding="utf-8") as file:
                rows = list(csv.DictReader(file))
            self.assertEqual(rows, [{"time": "t1", "battery_v": "3.7"}, {"time": "t2", "battery_v": ""}])

    def test_rows_are_on_disk_before_close(self):
        with tempfile.TemporaryDirectory() as directory:
            logger = pm.CsvLogger(directory, ["a"], stamp="x")
            logger.write({"a": 1})
            self.assertEqual(Path(logger.path).read_text(encoding="utf-8").splitlines(), ["a", "1"])
            logger.close()

    def test_nan_is_written_as_empty_and_unknown_fields_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            logger = pm.CsvLogger(directory, ["a"], stamp="y")
            logger.write({"a": math.nan})
            with self.assertRaises(ValueError):
                logger.write({"b": 1})
            logger.close()
            with logger.path.open(newline="", encoding="utf-8") as file:
                self.assertEqual(list(csv.DictReader(file)), [{"a": ""}])


if __name__ == "__main__":
    unittest.main()
