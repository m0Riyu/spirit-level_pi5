"""Dashboard contract plus real Chromium/IndexedDB behavior (no camera).

Chromium is optional on headless installs; the browser test reports a skip if
unavailable. This Pi has Chromium, so it runs in the normal unittest command.
"""
import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from urllib.request import urlopen

from websockets.sync.client import connect

import config


BROWSER_SCENARIOS = r"""
(async () => {
  // Exercise the LAN HTTP UUID fallback even though this fixture is a file URL.
  Object.defineProperty(crypto, "randomUUID", { value: undefined, configurable: true });
  const { PhoneLogStore, PhoneCaptureController, phoneCsv } = CaptureLog;
  const checks = [];
  const assert = (condition, label) => { if (!condition) throw new Error(label); checks.push(label); };
  assert(CaptureLog.taipeiIso(1767225600000) === "2026-01-01T08:00:00.000+08:00", "phone timestamps use Taipei independently of device timezone");
  const compactCard = document.querySelector('.capture-controls');
  assert(compactCard.children.length === 3 && compactCard.querySelectorAll('button').length === 2,
    "compact capture card only contains two buttons and count text");
  assert(compactCard.getBoundingClientRect().height <= 110, "capture controls stay flat at phone width");
  const wait = (ms) => new Promise(resolve => setTimeout(resolve, ms));
  const eventually = async (condition) => {
    for (let i = 0; i < 150; i++) { if (await condition()) return; await wait(15); }
    throw new Error("timed out waiting for browser state");
  };
  const makeUi = () => Object.fromEntries(["button", "export", "warning", "status", "count", "record"].map(name => {
    const node = document.createElement(name === "button" || name === "export" ? "button" : "div");
    document.body.appendChild(node); return [name, node];
  }));
  const reply = (code, data) => ({ ok: code >= 200 && code < 300, status: code, json: async () => data });
  const databaseName = "capture-browser-test-" + CaptureLog.uuid();
  let metric = 12.3, posts = [], polls = 0, release = false;
  const snapshot = () => ({ online: true, stable: false, fields: {
    message_rate_hz_at_press: metric, data_latency_median_ms_at_press: 8.5,
    connection_state_at_press: "connected", latest_telemetry_frame_id_at_press: 10,
    websocket_url: "ws://fake:8765", clock_offset_ms_at_press: 4.5,
  } });
  const ui = makeUi();
  ui.storage = document.createElement("div");
  const controller = new PhoneCaptureController({ snapshot, ui, store: new PhoneLogStore(databaseName), pollMs: 10,
    options: () => ({ reference_deg: -0.004, a_axis_deg: -0.012, sweep_direction: "forward", burst_frames: 3 }),
    fetcher: async (url, options) => {
      if (url.endsWith("ready")) return reply(200, { ready: true, require_stable_for_capture: false, burst_frames_default: 15,
        storage: { disk_free_mb: 97280, bytes_per_capture: 2 * 1024 ** 2, estimated_remaining_captures: 48128 } });
      if (options.method === "POST") {
        const body = JSON.parse(options.body); posts.push(body);
        metric = 999; // Metrics change before ack; frozen LOG must not change.
        if (posts.length === 1) throw new Error("POST ack lost");
        return reply(202, { status: "pending", request_id: body.request_id });
      }
      polls++;
      return reply(200, release ? { status: "saved", request_id: posts[0].request_id,
        session_id: "test_session", sample_id: 1, record_id: "test_session_000001", frame_id: 42, saved_at_epoch_ms: 1234 } :
        { status: "processing", request_id: posts[0].request_id });
    }
  });
  await controller.init();
  assert(!ui.button.disabled, "unstable capture allowed by default");
  await controller.press();
  await eventually(() => posts.length >= 2);
  assert(ui.button.disabled, "pending/uncertain status disables button");
  await controller.press();
  assert(posts.length === 2, "second press cannot enqueue another request");
  assert(Object.keys(posts[0]).join(",") === "request_id,reference_deg,a_axis_deg,sweep_direction,burst_frames",
    "POST sends request_id and capture options only, no phone metrics");
  assert(JSON.stringify(posts[0]) === JSON.stringify(posts[1]), "lost POST ack retries the identical body and request_id");
  assert(ui.storage.textContent.includes("約可再記錄 48128 次"), "remaining capture estimate shown");
  assert(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(posts[0].request_id), "UUID v4 format on HTTP");
  release = true;
  await eventually(async () => (await controller.store.all())[0]?.status === "saved");
  await eventually(() => controller.running.size === 0);
  const rows = await controller.store.all();
  assert(rows.length === 1, "saved reply updates same IndexedDB row");
  assert(rows[0].request_id === posts[0].request_id && rows[0].pi_frame_id === 42, "Pi IDs pair with frozen phone row");
  assert(rows[0].message_rate_hz_at_press === 12.3, "metrics frozen before asynchronous POST");
  assert(rows[0].reference_deg === -0.004 && rows[0].burst_frames === 3, "capture options stored in phone LOG row");
  assert(ui.status.textContent.includes("參考 -0.004°"), "saved message repeats the submitted reference value");
  for (const field of ["client_pressed_at_iso", "trigger_ack_received_at_iso", "pi_saved_response_received_at_iso"]) {
    assert(rows[0][field].endsWith("+08:00") && Date.parse(rows[0][field]) === rows[0][field.replace("_iso", "_epoch_ms")],
      `${field} uses Taipei offset and preserves epoch`);
  }
  assert(rows[0].button_to_saved_response_ms >= 0 && rows[0].status_poll_count >= 1, "monotonic completion and polling counters");
  assert(ui.record.textContent === "test_session_000001", "record_id shown on page");
  controller.store.db.close();
  const reopened = await new PhoneLogStore(databaseName).open();
  assert((await reopened.all()).length === 1, "IndexedDB survives close/reopen");

  const pendingId = CaptureLog.uuid();
  await reopened.put({ request_id: pendingId, status: "pending", status_poll_count: 0,
    client_pressed_at_epoch_ms: 2, message_rate_hz_at_press: 7.7, _page_id: "old-page", _pressed_perf_ms: 1 });
  const recoveryUi = makeUi();
  const recovery = new PhoneCaptureController({ snapshot, ui: recoveryUi, store: new PhoneLogStore(databaseName), pollMs: 10,
    fetcher: async (url, options) => {
      if (url.endsWith("ready")) return reply(200, { ready: true, require_stable_for_capture: false });
      if (options.method === "POST") throw new Error("recovery must query existing request");
      return reply(200, { status: "saved", request_id: pendingId, session_id: "s", sample_id: 2,
        record_id: "s_000002", frame_id: 50, saved_at_epoch_ms: 2345 });
    }
  });
  await recovery.init();
  await eventually(async () => (await reopened.all()).find(row => row.request_id === pendingId).status === "saved");
  assert((await reopened.all()).length === 2, "pending recovery updates existing key without duplication");
  const recovered = (await reopened.all()).find(row => row.request_id === pendingId);
  assert(recovered.message_rate_hz_at_press === 7.7 && recovered.button_to_saved_response_ms === null, "reload preserves metrics and does not invent monotonic duration");

  const invalidUi = makeUi();
  let invalidPosts = 0;
  const invalid = new PhoneCaptureController({ snapshot, ui: invalidUi, store: new PhoneLogStore(databaseName), pollMs: 10,
    options: () => { throw new Error("DL-S4W 讀值不是有效的角度"); },
    fetcher: async (url, options) => {
      if (url.endsWith("ready")) return reply(200, { ready: true });
      invalidPosts++; return reply(500, {});
    } });
  await invalid.init(); await invalid.press();
  assert(invalidPosts === 0 && invalidUi.status.textContent.includes("無法記錄") && invalid.running.size === 0,
    "invalid capture options block the trigger without a request");
  invalid.store.db.close();

  const errorUi = makeUi();
  const failure = new PhoneCaptureController({ snapshot, ui: errorUi, store: new PhoneLogStore(databaseName), pollMs: 10,
    fetcher: async (url, options) => {
      if (url.endsWith("ready")) return reply(200, { ready: true });
      const id = options.method === "POST" ? JSON.parse(options.body).request_id : url.split("/").pop();
      return reply(options.method === "POST" ? 202 : 200, options.method === "POST" ? { status: "pending", request_id: id } :
        { status: "error", request_id: id, error_code: "IMAGE_WRITE_FAILED", message: 'failed, "image"\nwrite' });
    }
  });
  await failure.init(); await failure.press();
  await eventually(async () => (await failure.store.all()).some(row => row.status === "error"));
  await eventually(() => failure.running.size === 0);
  const errorRow = (await failure.store.all()).find(row => row.status === "error");
  assert(errorRow.error_code === "IMAGE_WRITE_FAILED", "Pi error persisted in phone IndexedDB");
  assert(errorUi.count.textContent.includes("IMAGE_WRITE_FAILED"), "compact count line shows capture errors");

  recovery.requireStable = true;
  recovery.updateButton();
  assert(recoveryUi.button.disabled, "require_stable disables unstable frontend capture");
  recovery.httpReady = false;
  recovery.requireStable = false;
  recovery.updateButton();
  assert(recoveryUi.button.disabled, "HTTP unavailable disables capture");

  const unavailableUi = makeUi();
  const unavailable = new PhoneCaptureController({ snapshot, ui: unavailableUi,
    store: { open: async () => { throw new Error("IndexedDB denied"); } } });
  await unavailable.init();
  assert(unavailableUi.button.disabled && unavailableUi.status.textContent.includes("IndexedDB"), "IndexedDB unavailable shown and trigger disabled");
  const createUrl = URL.createObjectURL;
  URL.createObjectURL = () => { throw new Error("download unavailable"); };
  await failure.export();
  URL.createObjectURL = createUrl;
  assert(errorUi.status.textContent.includes("匯出失敗"), "CSV export failure visible");
  assert((await failure.store.all()).length === 3, "export does not delete logs");

  const csv = phoneCsv([
    { request_id: "saved", status: "saved", client_pressed_at_epoch_ms: 3 },
    { request_id: "error", status: "error", client_pressed_at_epoch_ms: 2, error_message: 'comma, quote"\nnewline' },
    { request_id: "pending", status: "pending", client_pressed_at_epoch_ms: 1 },
  ]);
  assert(csv.charCodeAt(0) === 0xFEFF && csv.indexOf('"pending"') < csv.indexOf('"saved"'), "UTF8 BOM CSV sorted by press time");
  assert(csv.includes('"comma, quote""\nnewline"'), "CSV escapes commas double quotes and newlines");
  const legacyCsv = phoneCsv([{request_id: "old", client_pressed_at_iso: "2026-01-01T00:00:00.000Z",
    client_pressed_at_epoch_ms: 1767225600000}]);
  assert(legacyCsv.includes("2026-01-01T08:00:00.000+08:00") && !legacyCsv.includes("00:00:00.000Z"),
    "existing UTC phone records export in Taipei timezone");
  assert(document.querySelector("#viewMeasure img, #viewMeasure video, #viewMeasure canvas") === null,
    "measurement view receives no image stream");
  assert(document.getElementById("ticksPreview").hidden && !document.getElementById("ticksPreview").hasAttribute("src"),
    "tick preview stays closed until requested");
  const optionsCard = document.getElementById("captureOptionsCard");
  assert(optionsCard.tagName === "DETAILS" && !optionsCard.open, "capture options card exists and is collapsed");
  for (const id of ["referenceDeg", "aAxisDeg", "sweepDirection", "burstFrames", "captureNote"]) {
    assert(optionsCard.contains(document.getElementById(id)), `${id} input inside options card`);
  }
  document.getElementById("referenceDeg").value = " -0.0051 ";
  document.getElementById("sweepDirection").value = "zero_check";
  document.getElementById("captureNote").value = "start";
  assert(JSON.stringify(captureOptions()) === JSON.stringify({ reference_deg: -0.0051, sweep_direction: "zero_check",
    note: "start", burst_frames: 15 }), "page reads typed capture options");
  document.getElementById("referenceDeg").value = "abc";
  let rejected = false;
  try { captureOptions(); } catch { rejected = true; }
  assert(rejected, "page rejects a non-numeric reference value");
  document.getElementById("referenceDeg").value = "";
  document.getElementById("burstFrames").value = "31";
  rejected = false;
  try { captureOptions(); } catch { rejected = true; }
  assert(rejected, "page rejects burst frames above 30");
  document.getElementById("burstFrames").value = "15";
  assert(document.getElementById("stabilityCard").tagName === "DETAILS", "collapsible stability card exists");
  assert(document.getElementById("captureButton").textContent === "記錄並拍照", "capture button exists");
  renderTelemetry({ type: "telemetry", frame_id: 42, sent_at_epoch_ms: Date.now(),
    system_state: "ADJUST", performance: { inference_ms: 80, fps: 10 },
    measurement: { valid: true, within_official_range: true, slope_mm_per_m: .04, offset_px: 36, offset_div: 2, angle_degrees: .002 },
    stability: { state: "STABLE", stable: true, sample_count: 20, valid_count: 20, valid_ratio: 1,
      mean_slope_mm_per_m: .04, std_slope_mm_per_m: .001, range_slope_mm_per_m: .003, stable_duration_seconds: 2 } });
  assert(document.getElementById("stabilityState").textContent === "穩定" && document.getElementById("state").textContent === "ADJUST", "stability independent of system level state");
  assert(document.getElementById("calibrationBanner").hidden, "no calibration banner without pending geometry");
  // Zero offset 0.427 div: slope +0.001 mm/m is 0.0438 div from the level point,
  // although the bubble is 0.4708 div right of the scale's geometric center.
  renderTelemetry({ type: "telemetry", frame_id: 44, sent_at_epoch_ms: Date.now(), system_state: "LEVEL",
    performance: {}, stability: {}, measurement: { valid: true, within_official_range: true, slope_mm_per_m: .001,
      offset_px: 8.47, offset_div: .4708, level_offset_div: .0438, angle_degrees: .0000573 } });
  assert(Math.abs(parseFloat(document.getElementById("bubble").style.left) - (50 + .0438 * 9)) < 1e-9,
    "bubble position uses zero-corrected divisions");
  assert(document.getElementById("divisions").textContent === "+0.044 div", "division text uses zero-corrected divisions");
  renderTelemetry({ type: "telemetry", frame_id: 45, sent_at_epoch_ms: Date.now(), system_state: "OUT_OF_RANGE",
    performance: {}, stability: {}, measurement: { valid: true, within_official_range: false, offset_px: 2,
      offset_div: .1, level_offset_div: -5.5 } });
  assert(document.getElementById("bubble").style.left === "5%", "out-of-range side follows zero-corrected sign");
  renderTelemetry({ type: "telemetry", frame_id: 43, sent_at_epoch_ms: Date.now(), system_state: "ADJUST",
    performance: {}, measurement: {}, stability: {},
    calibration: { geometry_version: "g1_geometry", vial_version: "v1_vial", geometry_pending_confirmation: true } });
  assert(!document.getElementById("calibrationBanner").hidden, "pending geometry shows the yellow banner");
  assert(document.getElementById("calibrationVersions").textContent.includes("v1_vial"), "active calibration versions shown");
  // Main menu, routes and the shared service mode.
  assert(!document.getElementById("viewMeasure").hidden && document.getElementById("viewMenu").hidden,
    "#/measure shows only the measurement view");
  assert(document.querySelectorAll("#viewMenu a.menu-item").length === 4, "main menu has ① ② ③ ⚙");
  applyState({ type: "state", mode: "ticks", pending_mode: null, changed_by: "other-phone", last_switch_ms: 31,
    calibration: { status: "pending", geometry: { version: "g2_geometry", pending_confirmation: true }, vial: { version: "v1_vial" } },
    system: { cpu_temperature_c: 51.24, git_commit: "abc1234", git_branch: "feature/integrated-modes" },
    camera: { open: true, open_count: 1, frame_id: 900, focus_absolute: 3711 } });
  assert(!document.getElementById("modeBanner").hidden &&
    document.getElementById("modeBannerText").textContent.includes("已被其他裝置切換為「③ 刻度檢查」"),
    "another phone's mode switch is announced on this page");
  assert(document.getElementById("statusCalibration").textContent === "待確認" &&
    document.getElementById("statusTemperature").textContent === "51.2 °C", "status bar shows calibration and temperature");
  assert(document.getElementById("systemStatus").textContent.includes("第 1 次開啟") &&
    document.getElementById("systemStatus").textContent.includes("abc1234"), "system page lists camera and version");
  applyState({ mode: "measure", pending_mode: null, changed_by: clientId });
  assert(document.getElementById("modeBanner").hidden, "banner clears when this page's mode is active");
  applyState({ mode: "align", pending_mode: "measure", changed_by: clientId });
  assert(document.getElementById("modeBanner").hidden, "no banner while this page's own switch is pending");
  location.hash = "#/system";
  await eventually(() => !document.getElementById("viewSystem").hidden);
  assert(document.getElementById("viewMeasure").hidden && document.getElementById("modeBanner").hidden,
    "system page does not claim a camera mode");
  // ③ tick check page with a faked server.
  const realFetch = window.fetch;
  const applyBodies = [];
  window.fetch = async (url, options = {}) => {
    if (url.startsWith("/api/ticks/abc123def456/apply")) {
      const body = JSON.parse(options.body); applyBodies.push(body);
      return body.confirm ? reply(200, { status: "applied", version: "20261007T010000_geometry", calibration: { status: "ok" } })
        : reply(409, { status: "error", error_code: "CONFIRMATION_REQUIRED", message: "需再次確認" });
    }
    if (url.startsWith("/api/calibration/geometry/") && url.endsWith("/activate")) return reply(200, { status: "ok" });
    if (url.startsWith("/api/calibration/geometry")) return reply(200, { kind: "geometry", active: {},
      versions: [{ version: "20261002T072923_geometry", active: false }, { version: "20261007T010000_geometry", active: true }] });
    return reply(404, {});
  };
  location.hash = "#/ticks";
  await eventually(() => !document.getElementById("viewTicks").hidden);
  tickMeasurementId = "abc123def456";
  renderTickResult({ id: "abc123def456", status: "done", frames_collected: 20, frames_target: 20, message: "",
    result: { checks: { tick_count: 26, expected_tick_count: 26, tick_count_ok: true, residual_rms_ok: true,
      max_residual_rms_px: .5, pitch_change_vs_previous: .051, pitch_change_ok: false, passed: true, needs_confirmation: true },
      fit_residual_rms_px: .16, fit_residual_max_px: .5, px_per_div_center: 18.92, previous_px_per_div_center: 18.0,
      scale_center_x_px: 381.12, previous_scale_center_x_px: 370.25, left_right_magnification_diff: -.0044,
      roll_deg: -.28, polynomial_degree: 2, inner_half_gap_div: 4.48, frames_used: 20 } });
  const resultText = document.getElementById("ticksResult").textContent;
  assert(resultText.includes("26 / 26 ✅") && resultText.includes("18.000 → 18.920（+5.10%）⚠️") &&
    resultText.includes("370.25 → 381.12 px（+10.87 px）") && resultText.includes("-0.28 °"), "tick result rows and marks");
  await applyTickResult();
  assert(document.getElementById("ticksApply").textContent.includes("再按一次") && applyBodies[0].confirm === false,
    "pitch change over the limit asks for a second press");
  await applyTickResult();
  assert(applyBodies[1].confirm === true && document.getElementById("ticksApply").textContent.includes("已套用"),
    "second press applies with confirmation");
  await refreshHistory();
  const history = document.getElementById("geometryHistory");
  assert(history.children.length === 2 && history.children[0].textContent.includes("（使用中）"), "history lists newest first");
  const rollback = history.querySelector("button");
  rollback.click();
  assert(rollback.textContent.includes("再按一次"), "rollback needs a second press");
  // ② alignment page.
  const alignSample = { type: "align", pose_count: 4, tag_count: 4, window_full: true, baseline_version: "a1_alignment",
    angles: { frames: 10, pitch_deg: 10.3, yaw_deg: 19.95, roll_deg: 179.5, pitch_deg_std: .004, yaw_deg_std: .006 },
    delta: { pitch_deg: .3, yaw_deg: -.05, roll_deg: .12 }, model_ready: true, tolerance_deg: .1,
    hold_seconds_required: 3, in_range_seconds: 0, can_complete: false, entered_range: false, teaching: {}, collecting: [],
    guidance: { within_tolerance: false, screws: [{ screw: "A", ok: false, label: "逆時針 約 1/4 圈" }, { screw: "B", ok: true, label: "✓" }] } };
  location.hash = "#/align";
  await eventually(() => !document.getElementById("viewAlign").hidden);
  renderAlign(alignSample);
  assert(document.querySelector("#alignPitch strong").textContent === "+0.300 °" &&
    document.getElementById("alignPitch").classList.contains("bad") &&
    document.getElementById("alignYaw").classList.contains("ok"), "large deltas colored against the tolerance");
  assert(document.getElementById("alignScrews").textContent.includes("螺絲 A　逆時針 約 1/4 圈") &&
    document.querySelector("#alignScrews .ok").textContent.includes("✓"), "screw guidance rows");
  assert(document.getElementById("alignRoll").textContent.includes("機構無法調整") && document.getElementById("alignComplete").disabled,
    "roll for reference; complete waits for the hold time");
  renderAlign({ ...alignSample, delta: { pitch_deg: .02, yaw_deg: -.05, roll_deg: .1 }, in_range_seconds: 3.2,
    can_complete: true, entered_range: true, guidance: { within_tolerance: true, screws: [] } });
  assert(!document.getElementById("alignComplete").disabled && document.getElementById("alignHold").textContent.includes("3.2 / 3"),
    "complete enabled after 3 s in range");
  renderAlign({ ...alignSample, delta: null, baseline_version: null, guidance: null });
  assert(document.querySelector("#alignPitch strong").textContent === "10.30 °" &&
    document.getElementById("alignStatus").textContent.includes("尚未設定基準"), "absolute angles without a baseline");
  window.fetch = realFetch;
  location.hash = "#/measure";
  await eventually(() => !document.getElementById("viewMeasure").hidden);
  for (const store of [reopened, recovery.store, failure.store]) store.db.close();
  return { checks, csv };
})()
"""


class DashboardContractTests(unittest.TestCase):
    def setUp(self):
        self.html = (config.APP_DIRECTORY / "dashboard/index.html").read_text()
        self.js = (config.APP_DIRECTORY / "dashboard/capture-log.js").read_text()

    def test_stability_details_and_capture_controls_exist(self):
        for value in ('id="stabilityCard"', 'id="captureButton"', 'id="exportPhoneLog"', 'id="phoneLogCount"'):
            self.assertIn(value, self.html)

    def test_no_image_stream_and_capture_option_inputs(self):
        measure_view = self.html[self.html.index('id="viewMeasure"'):self.html.index("<footer>")]
        for value in ("<img", "<video", "<canvas"):
            self.assertNotIn(value, measure_view)
        self.assertEqual(self.html.count("<img"), 2)  # ② and ③ previews, opened on request only
        for value in ('id="ticksPreview"', 'id="alignPreview"', 'id="alignPitch"', 'data-teach="A/start"',
                      '"/api/align/complete"', '"/api/align/baseline"'):
            self.assertIn(value, self.html)
        for value in ('id="captureOptionsCard"', 'id="referenceDeg"', 'id="aAxisDeg"', 'id="sweepDirection"',
                      'id="burstFrames"', 'id="captureNote"', 'id="calibrationBanner"'):
            self.assertIn(value, self.html)

    def test_main_menu_routes_and_mode_api(self):
        for value in ('id="viewMenu"', 'href="#/measure"', 'href="#/align"', 'href="#/ticks"', 'href="#/system"',
                      'id="viewAlign"', 'id="viewTicks"', 'id="viewSystem"', 'id="statusBar"', 'id="modeBanner"',
                      '"/api/mode"', '"/api/state"'):
            self.assertIn(value, self.html)

    def test_post_body_is_id_plus_options_and_indexeddb_key_is_id(self):
        self.assertIn('JSON.stringify(captureBody(row))', self.js)
        self.assertIn('keyPath: "request_id"', self.js)
        self.assertIn("indexedDB.open", self.js)

    def test_phone_metrics_are_raw_numeric_values(self):
        self.assertIn("message_rate_hz_at_press: metrics.rate", self.html)
        self.assertIn("data_latency_median_ms_at_press: metrics.latency", self.html)
        self.assertNotIn("parseFloat(ui.", self.html)


class BrowserCaptureTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("chromium"), "Chromium unavailable; browser scenarios need Chromium")
    def test_real_indexeddb_retry_recovery_export_and_ui(self):
        with tempfile.TemporaryDirectory() as profile:
            browser_log = open(Path(profile) / "browser_stderr.txt", "w+")
            binary = "/usr/lib/chromium/chromium" if Path("/usr/lib/chromium/chromium").is_file() else shutil.which("chromium")
            process = subprocess.Popen([binary, "--headless=new", "--disable-gpu",
                "--password-store=basic", "--disable-dev-shm-usage", "--no-first-run",
                "--no-default-browser-check", "--no-proxy-server",
                "--remote-debugging-port=0", f"--user-data-dir={profile}",
                (config.APP_DIRECTORY / "dashboard/index.html").as_uri() + "?wsPort=1#/measure"],
                stdout=subprocess.DEVNULL, stderr=browser_log)
            try:
                port_path = Path(profile) / "DevToolsActivePort"
                deadline = time.monotonic() + 20
                while not port_path.exists():
                    if process.poll() is not None or time.monotonic() > deadline:
                        browser_log.flush()
                        browser_log.seek(0)
                        self.fail("Chromium did not start: " + browser_log.read()[-3000:])
                    time.sleep(.05)
                port = int(port_path.read_text().splitlines()[0])
                with urlopen(f"http://127.0.0.1:{port}/json", timeout=3) as response:
                    target = next(item["webSocketDebuggerUrl"] for item in json.load(response) if item["type"] == "page")
                with connect(target, open_timeout=5, max_size=2 ** 22) as websocket:
                    serial = 0
                    def call(method, params):
                        nonlocal serial
                        serial += 1
                        call_id = serial
                        websocket.send(json.dumps({"id": call_id, "method": method, "params": params}))
                        while True:
                            result = json.loads(websocket.recv(timeout=30))
                            if result.get("id") == call_id:
                                self.assertNotIn("error", result)
                                return result["result"]
                    call("Emulation.setTimezoneOverride", {"timezoneId": "America/New_York"})
                    call("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True})
                    # Load the real assets directly; API responses are fakes,
                    # IndexedDB is genuine. LAN HTTP transport has separate API tests.
                    call("Runtime.evaluate", {"expression": """
                        new Promise(resolve => {
                          if (document.readyState === 'loading') {
                            document.addEventListener('DOMContentLoaded', () => resolve(true), {once:true});
                          } else resolve(true);
                        })
                    """, "awaitPromise": True, "returnByValue": True})
                    for _ in range(200):
                        result = call("Runtime.evaluate", {"expression": "document.readyState !== 'loading' && document.querySelector('#captureButton') !== null && typeof CaptureLog !== 'undefined' && typeof phoneCapture !== 'undefined'"})
                        if result.get("result", {}).get("value"): break
                        time.sleep(.025)
                    browser_log.flush()
                    browser_log.seek(0)
                    self.assertTrue(result.get("result", {}).get("value"), str(call("Runtime.evaluate", {
                        "expression": "JSON.stringify({url: location.href, body: document.body?.innerText.slice(0,500), scripts: Array.from(document.scripts, s=>s.src)})"})) + browser_log.read()[-3000:])
                    result = call("Runtime.evaluate", {"expression": BROWSER_SCENARIOS, "awaitPromise": True, "returnByValue": True})
                    self.assertNotIn("exceptionDetails", result, json.dumps(result, ensure_ascii=False))
                    value = result["result"]["value"]
                    self.assertGreaterEqual(len(value["checks"]), 25)
                    # Also parse exported CSV with Python's actual CSV reader.
                    import csv
                    import io
                    rows = list(csv.DictReader(io.StringIO(value["csv"].lstrip("\ufeff"))))
                    self.assertEqual([row["status"] for row in rows], ["pending", "error", "saved"])
                    self.assertEqual(rows[1]["error_message"], 'comma, quote"\nnewline')
                    print(f"Chromium: {len(value['checks'])} browser/IndexedDB assertions passed")
            finally:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                browser_log.close()


if __name__ == "__main__":
    unittest.main()
