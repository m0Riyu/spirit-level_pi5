/* Numeric phone logs stay on this browser. Only request_id is POSTed to Pi. */
"use strict";
(() => {
  const PHONE_LOG_FIELDS = (
    "request_id session_id sample_id record_id pi_frame_id status error_code error_message " +
    "client_pressed_at_iso client_pressed_at_epoch_ms " +
    "trigger_ack_received_at_iso trigger_ack_received_at_epoch_ms " +
    "pi_saved_response_received_at_iso pi_saved_response_received_at_epoch_ms " +
    "connection_state_at_press websocket_url latest_telemetry_frame_id_at_press " +
    "latest_telemetry_sent_at_epoch_ms latest_telemetry_received_at_epoch_ms " +
    "message_rate_hz_at_press data_latency_median_ms_at_press total_latency_median_ms_at_press " +
    "inference_median_ms_at_press clock_offset_ms_at_press " +
    "trigger_ack_ms button_to_saved_response_ms status_poll_count"
  ).split(" ");
  const TERMINAL = new Set(["saved", "rejected", "error"]);

  function taipeiIso(epochMs = Date.now()) {
    return new Date(Number(epochMs) + 8 * 60 * 60 * 1000).toISOString().slice(0, -1) + "+08:00";
  }

  function normalizePhoneTimes(row) {
    const normalized = { ...row };
    for (const field of PHONE_LOG_FIELDS.filter(name => name.endsWith("_at_iso"))) {
      const epoch = row[field.slice(0, -4) + "_epoch_ms"];
      const numeric = epoch == null ? Date.parse(row[field]) : Number(epoch);
      if (Number.isFinite(numeric)) normalized[field] = taipeiIso(numeric);
    }
    return normalized;
  }

  function uuid() {
    // crypto.randomUUID needs HTTPS; getRandomValues also works on a LAN HTTP URL.
    if (crypto.randomUUID) return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }

  function phoneCsv(rows) {
    const escape = (value) => `"${String(value ?? "").replaceAll('"', '""')}"`;
    const sorted = [...rows].sort((a, b) => a.client_pressed_at_epoch_ms - b.client_pressed_at_epoch_ms);
    return "\uFEFF" + [PHONE_LOG_FIELDS, ...sorted.map(normalizePhoneTimes).map((row) => PHONE_LOG_FIELDS.map((name) => row[name]))]
      .map((row) => row.map(escape).join(",")).join("\r\n") + "\r\n";
  }

  class PhoneLogStore {
    constructor(name = "bubble-phone-captures-v1") { this.name = name; this.db = null; }
    open() {
      return new Promise((resolve, reject) => {
        if (!window.indexedDB) { reject(new Error("IndexedDB 不可用")); return; }
        const request = indexedDB.open(this.name, 1);
        request.onupgradeneeded = () => {
          request.result.createObjectStore("captures", { keyPath: "request_id" });
        };
        request.onerror = () => reject(request.error);
        request.onblocked = () => reject(new Error("請關閉其他使用此網站的分頁後再開啟"));
        request.onsuccess = () => {
          this.db = request.result;
          this.db.onversionchange = () => this.db.close();
          resolve(this);
        };
      });
    }
    transaction(mode, action) {
      return new Promise((resolve, reject) => {
        const transaction = this.db.transaction("captures", mode);
        let result;
        transaction.oncomplete = () => resolve(result);
        transaction.onerror = () => reject(transaction.error || new Error("手機 LOG 寫入失敗"));
        transaction.onabort = () => reject(transaction.error || new Error("手機 LOG 交易中止"));
        action(transaction.objectStore("captures"), (value) => { result = value; });
      });
    }
    put(row) {
      return this.transaction("readwrite", (store, done) => {
        const normalized = normalizePhoneTimes(row);
        store.put(normalized).onsuccess = () => done(normalized);
      });
    }
    update(requestId, changes) {
      return this.transaction("readwrite", (store, done) => {
        store.get(requestId).onsuccess = (event) => {
          const previous = event.target.result;
          if (!previous) throw new Error("手機 LOG request 查無紀錄");
          const row = normalizePhoneTimes({ ...previous, ...changes, request_id: requestId });
          store.put(row).onsuccess = () => done(row);
        };
      });
    }
    all() {
      return this.transaction("readonly", (store, done) => {
        store.getAll().onsuccess = (event) => done(event.target.result);
      });
    }
  }

  class PhoneCaptureController {
    constructor({ snapshot, ui, store = new PhoneLogStore(), pollMs = 900, fetcher = window.fetch.bind(window) }) {
      this.snapshot = snapshot;
      this.ui = ui;
      this.store = store;
      this.pollMs = pollMs;
      this.fetcher = fetcher;
      this.storageReady = false;
      this.httpReady = false;
      this.requireStable = false;
      this.running = new Set();
      this.pressing = false;
      this.countSummary = "手機端紀錄：0 筆 · 已配對：0 筆 · 等待中：0 筆 · 失敗：0 筆";
      this.statusMessage = "";
      this.errorMessage = "";
      this.pageId = uuid();
      this.ui.button.disabled = true;
      this.ui.button.addEventListener("click", () => this.press());
      this.ui.export.addEventListener("click", () => this.export());
    }
    async init() {
      try {
        await this.store.open();
        this.storageReady = true;
        const rows = await this.refreshCounts();
        const pending = rows.filter((row) => !TERMINAL.has(row.status));
        if (pending.length) this.notify("恢復查詢先前尚未完成的要求…");
        // Reserve every pending ID before enabling a new press.
        for (const row of pending) this.running.add(row.request_id);
        for (const row of pending) this.run(row, false);
        await this.refreshReady();
      } catch (error) { this.storageError(error); }
      this.updateButton();
    }
    storageError(error) {
      this.storageReady = false;
      this.notify(`手機 LOG 無法保存：${error.message}。請確認瀏覽器儲存權限。`, true);
      this.updateButton();
    }
    async refreshReady() {
      try {
        const response = await this.fetchJson("/api/captures/ready");
        this.httpReady = response.ok && response.data.ready === true;
        if (response.ok) this.requireStable = response.data.require_stable_for_capture === true;
      } catch { this.httpReady = false; }
      this.updateButton();
    }
    updateButton() {
      const snapshot = this.snapshot();
      this.ui.button.disabled = !this.storageReady || !this.httpReady || !snapshot.online ||
        this.pressing || this.running.size > 0 || (this.requireStable && !snapshot.stable);
      const warning = snapshot.stable ? "氣泡穩定，可記錄。" :
        (this.requireStable ? "必須等待氣泡穩定才能記錄。" : "氣泡尚未穩定；仍可記錄，Pi LOG 將保存當下狀態。");
      if (this.ui.warning) this.ui.warning.textContent = warning;
      this.ui.button.title = [warning, this.statusMessage].filter(Boolean).join("\n");
    }
    notify(message, error = false) {
      this.statusMessage = message;
      this.errorMessage = error ? message : "";
      if (this.ui.status) this.ui.status.textContent = message;
      this.renderCount();
    }
    renderCount() {
      this.ui.count.textContent = this.countSummary + (this.errorMessage ? ` · ${this.errorMessage}` : "");
      this.ui.count.title = this.statusMessage;
    }
    async refreshCounts() {
      let rows;
      try { rows = await this.store.all(); }
      catch (error) { this.storageError(error); throw error; }
      const saved = rows.filter((row) => row.status === "saved");
      const waiting = rows.filter((row) => !TERMINAL.has(row.status));
      this.countSummary = `手機端紀錄：${rows.length} 筆 · 已配對：${saved.length} 筆 · 等待中：${waiting.length} 筆 · 失敗：${rows.length - saved.length - waiting.length} 筆`;
      this.renderCount();
      saved.sort((a, b) => b.client_pressed_at_epoch_ms - a.client_pressed_at_epoch_ms);
      if (this.ui.record) this.ui.record.textContent = saved[0]?.record_id || "—";
      return rows;
    }
    async updateRow(requestId, changes) {
      try { return await this.store.update(requestId, changes); }
      catch (error) { this.storageError(error); throw error; }
    }
    async press() {
      if (this.ui.button.disabled || this.pressing || this.running.size) return;
      // Freeze numeric metrics synchronously at the press, before any await/POST.
      this.pressing = true;
      const epoch = Date.now();
      const pressedPerf = performance.now();
      const snapshot = this.snapshot();
      const row = Object.fromEntries(PHONE_LOG_FIELDS.map((name) => [name, null]));
      Object.assign(row, snapshot.fields, {
        request_id: uuid(), status: "pending", status_poll_count: 0,
        client_pressed_at_iso: taipeiIso(epoch), client_pressed_at_epoch_ms: epoch,
        // Internal-only timing fields, not exported or transmitted.
        _page_id: this.pageId, _pressed_perf_ms: pressedPerf,
      });
      this.running.add(row.request_id);
      this.updateButton();
      this.notify("已凍結手機數值，送出拍攝要求…");
      try {
        await this.store.put(row); // Never trigger Pi unless phone pending row is durable.
        await this.refreshCounts();
        this.pressing = false;
        this.run(row, true);
      } catch (error) {
        this.running.delete(row.request_id);
        this.pressing = false;
        this.storageError(error);
      }
    }
    async fetchJson(url, options = {}) {
      const abort = new AbortController();
      const timeout = setTimeout(() => abort.abort(), 5000);
      try {
        const response = await this.fetcher(url, { ...options, cache: "no-store", signal: abort.signal });
        return { ok: response.ok, code: response.status, data: await response.json() };
      } finally { clearTimeout(timeout); }
    }
    elapsed(row) {
      // A new page has a new performance clock. Leave duration empty after a
      // reload rather than substituting wall-clock differences as monotonic time.
      return row._page_id === this.pageId ? performance.now() - row._pressed_perf_ms : null;
    }
    async run(initial, postFirst) {
      let row = initial;
      let post = postFirst;
      try {
        while (true) {
          try {
            let response;
            const wasPost = post;
            if (post) {
              response = await this.fetchJson("/api/captures", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ request_id: row.request_id }),
              });
              post = false;
              if (response.ok) {
                const epoch = Date.now();
                row = await this.updateRow(row.request_id, {
                  trigger_ack_received_at_iso: normalizePhoneTimes(row).trigger_ack_received_at_iso || taipeiIso(epoch),
                  trigger_ack_received_at_epoch_ms: row.trigger_ack_received_at_epoch_ms || epoch,
                  trigger_ack_ms: row.trigger_ack_ms ?? (response.code === 202 ? this.elapsed(row) : null),
                });
              } else if ([400, 403, 409, 429].includes(response.code)) {
                await this.finish(row, { status: "error", request_id: row.request_id,
                  error_code: response.data.error_code, message: response.data.message });
                return;
              }
            } else {
              row = await this.updateRow(row.request_id, { status_poll_count: row.status_poll_count + 1 });
              response = await this.fetchJson(`/api/captures/${encodeURIComponent(row.request_id)}`);
              if (response.code === 404) { post = true; }
            }
            if (response.ok && response.data.request_id === row.request_id) {
              if (TERMINAL.has(response.data.status) && !wasPost) {
                await this.finish(row, response.data);
                return;
              }
              if (["pending", "processing"].includes(response.data.status)) {
                row = await this.updateRow(row.request_id, { status: response.data.status });
                this.notify(response.data.status === "processing" ? "Pi 正在處理並保存同幀 ROI…" : "等待 Pi 下一個完整處理幀…");
              }
            }
          } catch (error) {
            // Network uncertainty keeps the original ID pending. A GET 404
            // later retries POST with that SAME ID; never create a second row.
            if (!this.storageReady) throw error;
            this.notify("連線或儲存回應尚未確認，使用原 request_id 繼續查詢…");
          }
          await new Promise((resolve) => setTimeout(resolve, this.pollMs));
        }
      } catch (error) { this.storageError(error); }
      finally {
        this.running.delete(row.request_id);
        this.updateButton();
      }
    }
    async finish(row, response) {
      const epoch = Date.now();
      const changes = {
        status: response.status, error_code: response.error_code || null, error_message: response.message || null,
      };
      if (response.status === "saved") {
        Object.assign(changes, {
          session_id: response.session_id, sample_id: response.sample_id,
          record_id: response.record_id, pi_frame_id: response.frame_id,
          pi_saved_response_received_at_iso: taipeiIso(epoch),
          pi_saved_response_received_at_epoch_ms: epoch,
          button_to_saved_response_ms: this.elapsed(row),
        });
      }
      await this.updateRow(row.request_id, changes);
      await this.refreshCounts();
      this.notify(response.status === "saved" ? `保存成功：${response.record_id}` :
        `拍攝${response.status === "rejected" ? "被拒絕" : "失敗"}：${response.error_code} · ${response.message}`, response.status !== "saved");
    }
    async export() {
      this.ui.export.disabled = true;
      try {
        const rows = await this.store.all();
        const blob = new Blob([phoneCsv(rows)], { type: "text/csv;charset=utf-8" });
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        const stamp = taipeiIso().replace(/[-:]/g, "").replace("T", "_").slice(0, 15);
        link.download = `phone_capture_log_${stamp}.csv`;
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 10000);
        this.notify("已匯出手機端 LOG；瀏覽器紀錄仍保留，請確認下載檔案。");
      } catch (error) { this.notify(`手機 LOG 匯出失敗：${error.message}`, true); }
      finally { this.ui.export.disabled = false; }
    }
  }
  window.CaptureLog = { PHONE_LOG_FIELDS, PhoneLogStore, PhoneCaptureController, phoneCsv, taipeiIso, uuid };
})();
