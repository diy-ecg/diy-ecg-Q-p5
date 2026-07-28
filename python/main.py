"""
WebUI/Socket.IO-based ECG viewer (optimized for browser-side throttling).
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from arduino.app_bricks.web_ui import WebUI
from arduino.app_utils import App

from arduino_q_bridge_rpc import ArduinoQRpcClient, open_rpc
from data_stream import DataStream

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ecg")

PLOT_WINDOW = 800
STREAM_LENGTH = 4000
# Polling every 40ms --> 25 FPS
POLL_INTERVAL_S = 0.05
ADAPTIVE_MEAN_FAST = True
# Log a heartbeat every N seconds so a stalled data stream is visible even
# without errors (e.g. MCU stopped sampling but RPC calls still succeed).
HEARTBEAT_INTERVAL_S = 5.0


class ECGWebServer:
    def __init__(self) -> None:
        assets_dir = Path(__file__).resolve().parent / "webui"
        self.ui = WebUI(assets_dir_path=str(assets_dir))

        self.ui.on_connect(self._on_connect)
        self.ui.on_disconnect(self._on_disconnect)
        self.ui.on_message("set_filters", self._wrap(self._handle_set_filters))
        self.ui.on_message("clear_buffer", self._wrap(self._handle_clear_buffer))
        self.ui.on_message("request_status", self._wrap(self._handle_request_status))

        self.stream = DataStream(
            name="EKG",
            length=STREAM_LENGTH,
            adaptive_fast=ADAPTIVE_MEAN_FAST,
        )
        self.client: ArduinoQRpcClient | None = None

        self.status = "Not connected"
        self.t0: float | None = None
        self.last_count = 0
        self.last_bpm: int | None = None
        self.last_polarity: int | None = None
        self.last_sampling_rate: float | None = None
        self.filters = {"hp": True, "no": True, "tp": True, "am": True}

        self.last_payload: dict[str, Any] | None = None
        self.last_meta: dict[str, Any] | None = None
        self.lock = threading.RLock()
        self.stop_event = threading.Event()

        self.poll_thread = threading.Thread(target=self._poll_loop, daemon=True)

        # Diagnostics: only log status changes (not every poll), plus a
        # periodic heartbeat so a silently stalled stream is still visible.
        self._logged_status: str | None = None
        self._next_heartbeat = time.monotonic()
        self._samples_since_heartbeat = 0

    def start(self) -> None:
        log.info("stage=startup poll_interval_s=%.3f", POLL_INTERVAL_S)
        self.poll_thread.start()
        App.run()

    def _wrap(self, fn):
        def wrapper(*args):
            if len(args) == 2:
                return fn(args[0], args[1] or {})
            return fn(None, {})
        return wrapper

    def _poll_loop(self) -> None:
        next_tick = time.monotonic()
        while not self.stop_event.is_set():
            self._poll_once()
            self._log_heartbeat()
            next_tick += POLL_INTERVAL_S
            sleep_s = next_tick - time.monotonic()
            if sleep_s > 0:
                time.sleep(sleep_s)

    def _set_status(self, status: str) -> None:
        self.status = status
        if status != self._logged_status:
            log.info("stage=bridge_rpc status=%s", status)
            self._logged_status = status

    def _log_heartbeat(self) -> None:
        now = time.monotonic()
        if now < self._next_heartbeat:
            return
        self._next_heartbeat = now + HEARTBEAT_INTERVAL_S
        log.info(
            "stage=poll_loop samples_last_%.0fs=%d status=%s bpm=%s",
            HEARTBEAT_INTERVAL_S,
            self._samples_since_heartbeat,
            self.status,
            self.last_bpm,
        )
        self._samples_since_heartbeat = 0

    def _poll_once(self) -> bool:
        if self.client is None:
            try:
                self.client = open_rpc()
                self._set_status("Connected (Bridge RPC)")
            except Exception as exc:
                self._set_status(f"RPC error: {exc}")
                return False

        try:
            samples, timestamps = self.client.request_frame()
        except Exception as exc:
            self._set_status(f"RPC error: {exc}")
            self.client = None
            return False

        if not samples:
            return False

        self._samples_since_heartbeat += len(samples)

        with self.lock:
            if self.t0 is None and timestamps:
                self.t0 = float(timestamps[0])

            self.stream.set_filter_enabled(**self.filters)
            self.stream.add_samples(samples, timestamps)
            self.last_count = len(samples)

            bpm_data = self.stream.consume_new_bpm()
            if bpm_data:
                self.last_bpm, self.last_polarity = bpm_data

            if len(timestamps) >= 2:
                span_ms = timestamps[-1] - timestamps[0]
                if span_ms > 0:
                    self.last_sampling_rate = (len(timestamps) - 1) / (span_ms / 1000.0)

            meta = {
                "status": self.status,
                "last_count": self.last_count,
                "bpm": self.last_bpm,
                "polarity": self.last_polarity,
                "filters": self.filters,
                "sampling_rate_hz": self.last_sampling_rate,
            }

            delta = self._build_delta_payload(
                *self.stream.last(self.last_count), self.stream.dynamic_threshold
            )
            if not delta:
                return False

            self.last_meta = meta

        self.ui.send_message("ecg_meta", meta)
        self.ui.send_message("ecg_delta", delta)
        return True

    def _on_connect(self, sid: str) -> None:
        log.info("stage=browser_socket connected sid=%s", sid)
        with self.lock:
            meta = self.last_meta
            payload = self._build_full_payload()
        if meta:
            self.ui.send_message("ecg_meta", meta, room=sid)
        if payload:
            self.ui.send_message("ecg_frame", payload, room=sid)

    def _on_disconnect(self, sid: str) -> None:
        log.info("stage=browser_socket disconnected sid=%s", sid)

    def _handle_set_filters(self, _sid, payload):
        with self.lock:
            for k in self.filters:
                if k in payload:
                    self.filters[k] = bool(payload[k])
        return {"filters": self.filters}

    def _handle_clear_buffer(self, _sid, _payload):
        with self.lock:
            self.stream = DataStream(
                name="EKG",
                length=STREAM_LENGTH,
                adaptive_fast=ADAPTIVE_MEAN_FAST,
            )
            self.t0 = None
            self.last_payload = None
            self.last_meta = None
        return {"cleared": True}

    def _handle_request_status(self, _sid, _payload):
        with self.lock:
            payload = self._build_full_payload()
        return payload or {"status": self.status, "filters": self.filters}

    def _build_delta_payload(
        self, samples: list[float], timestamps: list[float], threshold: float | None
    ) -> dict[str, Any] | None:
        count = len(samples)
        if count == 0:
            return None
        t0 = int(round(timestamps[0]))
        dts = [0] * count
        prev_t = t0
        for i in range(1, count):
            dt = int(round(timestamps[i] - prev_t))
            if dt < 0:
                dt = 0
            elif dt > 255:
                dt = 255
            dts[i] = dt
            prev_t = int(round(timestamps[i]))

        return {
            "t0": t0,
            "dt": dts,
            "y": [float(val) for val in samples],
            "threshold": threshold,
        }

    def _build_full_payload(self) -> dict[str, Any] | None:
        y, t = self.stream.last(PLOT_WINDOW)
        if not y:
            return None
        if self.t0 is None and t:
            self.t0 = float(t[0])
        t0 = self.t0 or 0.0
        return {
            "plot_window": PLOT_WINDOW,
            "signal": {
                "t0": t0,
                "t": [ts - t0 for ts in t],
                "y": list(y),
                "threshold": self.stream.dynamic_threshold,
            },
        }


def main() -> None:
    ECGWebServer().start()


if __name__ == "__main__":
    main()
