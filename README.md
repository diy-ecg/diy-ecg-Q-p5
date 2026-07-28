# DIY-ECG

ECG acquisition and visualization on the Arduino UNO Q. The solution is built
from three main parts that form a simple end-to-end chain:

1. MCU sketch (STM32U585)
2. MPU Linux Python scripts (UNO Q Linux)
3. Browser JS scripts (WebUI)

## Architecture and interaction

### 1) MCU sketch
- File: `sketch/sketch.ino`
- Role: Samples ADC values at 200 Hz and exposes frames via the UNO Q
  Bridge/RPC call `ecg_get_frame`.
- Output: Compact frames with timestamps that the Linux side can fetch.

### 2) MPU Linux Python scripts
- Entry point: `python/main.py`
- Data pipeline: `python/data_stream.py`
- Transport/RPC: `python/arduino_q_bridge_rpc.py`
- Role: Pulls frames over Bridge/RPC, applies filters (notch, low-pass,
  high-pass), computes BPM/R-peak detection, and keeps a buffer for plotting.
- Output: Streams three WebSocket message types to the browser:
  `ecg_frame` (full buffer for new clients), `ecg_delta` (incremental updates),
  and `ecg_meta` (status, filters, BPM, sampling rate).

### 3) Browser JS scripts
- File: `python/webui/app.js` (`index.html` stays empty; all DOM/UI is built
  by p5.js in `setup()`)
- Role: Builds the UI, receives WebSocket updates, plots samples, and shows
  status/BPM/filter state. The view updates continuously without refresh.
- Plotting uses [p5.js](https://p5js.org/) (`createCanvas`/`draw()`), redrawn
  at a fixed 10 FPS via `frameRate()`.
- The UI consumes `ecg_frame` for initial state, then applies `ecg_delta`
  updates while reflecting `ecg_meta` in the status panel.

## Data flow (short)

1. MCU provides samples + timestamps via `ecg_get_frame`.
2. Python fetches frames, filters/analyzes, and produces `ecg_frame`,
   `ecg_delta`, and `ecg_meta`.
3. WebUI receives `ecg_frame` on connect, then applies `ecg_delta` and updates
   status from `ecg_meta`.

## Ring buffers, rates, and binary frames

The pipeline is designed around small ring buffers at every stage to keep
memory bounded while supporting steady real-time updates.

### MCU ring buffer + sampling rate
- Sampling rate: 200 Hz on the MCU.
- The sketch accumulates samples into fixed-size frames and exposes them through
  `ecg_get_frame`. Each frame is a compact binary payload that includes a start
  timestamp plus per-sample timestamp deltas, keeping bandwidth low.

### MPU (Linux) ring buffer + poll rate
- Poll rate: ~20–25 FPS (`POLL_INTERVAL_S = 0.05` in `python/main.py`).
- `DataStream` in `python/data_stream.py` maintains a circular buffer of the
  latest samples and timestamps for filtering and BPM detection.
- The Python side emits "delta" payloads instead of full frames for efficient
  UI updates.

### Browser ring buffer + refresh rate
- The WebUI maintains its own buffer of the last few seconds of data for
  plotting, refreshing the chart at the UI frame rate.
- Incoming WebSocket messages carry only the newest samples plus timestamp
  deltas, which the browser expands into absolute timestamps as needed.

### Binary frames with timestamps and diffs
- Frame layout (binary): `[count][t0][samples + dt]`
- `t0` stores the absolute start timestamp in milliseconds.
- Each sample includes a compact time delta (`dt`) from the previous sample,
  which keeps payloads small while preserving timing.

## Running the app

1. Start sketch + Python via Arduino App CLI:
   ```bash
   arduino-app-cli app start .
   ```
2. Open the UI in a browser:
   - Local on the UNO Q: `http://localhost:7000`
   - With port forwarding: `http://127.0.0.1:7000`

## Diagnosing a stalled data stream

`python/main.py` logs each pipeline stage to the console (via `arduino-app-cli
app start .` or `arduino-app-cli app logs`):
- `stage=bridge_rpc` — connection/error state of the Bridge/RPC call to the MCU.
- `stage=poll_loop` — a heartbeat every 5 s with samples received and current BPM.
- `stage=browser_socket` — WebUI clients connecting/disconnecting.

If the browser shows "Disconnected", check the logs to see which stage last
reported activity: no `bridge_rpc`/`poll_loop` lines means the MCU/RPC link is
stuck; a `browser_socket disconnected` with `poll_loop` still ticking means the
backend is fine and the issue is the browser/network side.

## Git/SSH setup (multiple UNO Q devices)

If you work from multiple UNO Q devices, use SSH keys for GitHub access. You
can reuse a single key across devices, but the safer approach is one key per
device so individual keys can be revoked independently.
