"""
Core data stream logic: ring buffer, filtering, adaptive mean, and R-peak/BPM
detection implemented with plain Python lists so we can run without NumPy.
"""
from __future__ import annotations

import math
from collections import deque


class IIRFilter:
    """Simple direct-form-II transposed IIR filter with persistent state."""

    def __init__(self, b: list[float], a: list[float], zi: list[float]) -> None:
        self.b = [float(val) for val in b]
        self.a = [float(val) for val in a]
        if not math.isclose(self.a[0], 1.0, rel_tol=1e-9, abs_tol=1e-9):
            scale = self.a[0]
            self.b = [val / scale for val in self.b]
            self.a = [val / scale for val in self.a]
        self.state = [float(val) for val in zi]

    def filter(self, samples: list[float]) -> list[float]:
        """Filter the provided samples and return a new list."""
        if not samples:
            return []
        y = [0.0] * len(samples)
        z = self.state.copy()
        order = len(z)
        for idx, xn in enumerate(samples):
            accumulator = z[0] if order else 0.0
            yn = self.b[0] * xn + accumulator
            y[idx] = yn
            for i in range(order - 1):
                z[i] = self.b[i + 1] * xn + z[i + 1] - self.a[i + 1] * yn
            if order:
                z[-1] = self.b[-1] * xn - self.a[-1] * yn
        self.state = z
        return y


class DataStream:
    def __init__(
        self,
        name: str = "EKG",
        length: int = 2000,
        fs: float = 200.0,
        adaptive_fast: bool = True,
    ) -> None:
        self.name = name
        self.length = length
        self.fs = fs
        self.adaptive_fast = adaptive_fast
        # Ring buffer (circular overwrite) for samples/timestamps
        self.samples = [0.0] * self.length
        self.timestamps = [0.0] * self.length
        self.write_idx = 0
        self.filled = 0

        # Filter enable flags
        self.hp_enabled = True
        self.no_enabled = True
        self.tp_enabled = True
        self.am_enabled = True

        # IIR filters (coefficients precomputed for fs=200 Hz)
        self.hp_filter = IIRFilter(
            b=[0.95654323, -1.91308645, 0.95654323],
            a=[1.0, -1.91119707, 0.91497583],
            zi=[-0.95654323, 0.95654323],
        )
        self.no_filter = IIRFilter(
            b=[0.974482283, -1.19339661e-16, 0.974482283],
            a=[1.0, -1.19339661e-16, 0.948964567],
            zi=[0.02551772, 0.02551772],
        )
        self.tp_filter = IIRFilter(
            b=[0.20657208, 0.41314417, 0.20657208],
            a=[1.0, -0.36952738, 0.19581571],
            zi=[0.79342792, 0.01075637],
        )

        # Adaptive mean / R-peak detection state
        self.window_size = int(round(0.2 * self.fs))
        self.inhibit_time = int(round(0.05 * self.fs))
        self.max_window_size = int(round(2 * self.fs))
        self.buffer = [0.0] * self.window_size
        self.buffer_index = 0
        self.sum_val = 0.0
        self.filter_disable = False
        self.inhibit_counter = 0
        self.max_window = deque()
        self.max_window_max = deque()
        self.max_window_min = deque()
        self.max_window_sum = 0.0
        self.max_buffer = [0.0] * self.max_window_size
        self.max_index = 0
        self.peak_polarity = 1
        self.last_r_polarity = 1
        self.last_r_peak_time = 0.0
        self.prev_r_peak_time = 0.0
        self.BPM = 0
        self.newBPM = False
        self.dynamic_threshold: float | None = None
        self.last_bpm = 0

    def set_filter_enabled(
        self, hp: bool | None = None, no: bool | None = None, tp: bool | None = None, am: bool | None = None
    ) -> None:
        if hp is not None:
            self.hp_enabled = hp
        if no is not None:
            self.no_enabled = no
        if tp is not None:
            self.tp_enabled = tp
        if am is not None:
            self.am_enabled = am

    def add_samples(self, samples: list[int], timestamps: list[int]) -> None:
        """Append samples/timestamps to the ring buffer with optional filtering and BPM detection."""
        if not samples:
            return
        x = [float(val) for val in samples]
        t_arr = [float(val) for val in timestamps]

        # Notch -> Lowpass -> Highpass (same order as Octave)
        if self.no_enabled:
            x = self.no_filter.filter(x)
        if self.tp_enabled:
            x = self.tp_filter.filter(x)
        if self.hp_enabled:
            x = self.hp_filter.filter(x)

        filtered = [0.0 if abs(val) < 0.001 else val for val in x]

        for s_val, t_val in zip(filtered, t_arr):
            # Adaptive mean and R-peak detection per sample
            y = self._process_adaptive_mean(s_val, t_val) if self.am_enabled else s_val
            self._append_sample(float(y), float(t_val))

    def _append_sample(self, sample: float, timestamp: float) -> None:
        """Write one sample into the circular buffer."""
        self.samples[self.write_idx] = sample
        self.timestamps[self.write_idx] = timestamp
        self.write_idx = (self.write_idx + 1) % self.length
        if self.filled < self.length:
            self.filled += 1

    def _process_adaptive_mean(self, sample: float, timestamp: float) -> float:
        # Update dynamic threshold based on last 2 seconds.
        if self.adaptive_fast:
            self._update_window_stats_fast(sample)
            local_max = self.max_window_max[0]
            local_min = self.max_window_min[0]
            local_mean = self.max_window_sum / len(self.max_window)
        else:
            self.max_buffer[self.max_index] = sample
            self.max_index = (self.max_index + 1) % self.max_window_size
            local_max = max(self.max_buffer)
            local_min = min(self.max_buffer)
            local_mean = sum(self.max_buffer) / len(self.max_buffer)
        dist_max = local_max - local_mean
        dist_min = local_mean - local_min

        if dist_max >= dist_min:
            self.peak_polarity = 1
            self.dynamic_threshold = local_mean + 0.5 * dist_max
            is_r_candidate = sample > self.dynamic_threshold
        else:
            self.peak_polarity = -1
            self.dynamic_threshold = local_mean - 0.5 * dist_min
            is_r_candidate = sample < self.dynamic_threshold

        if is_r_candidate and (timestamp - self.last_r_peak_time) > 250.0:
            self.prev_r_peak_time = self.last_r_peak_time
            self.last_r_peak_time = timestamp
            if self.prev_r_peak_time > 0:
                rr_interval = self.last_r_peak_time - self.prev_r_peak_time
                if rr_interval > 0:
                    self.BPM = int(round(60000.0 / rr_interval))
                    self.newBPM = True
                    self.last_r_polarity = self.peak_polarity
            self.filter_disable = True
            self.inhibit_counter = self.inhibit_time

        if self.filter_disable and self.inhibit_counter > 0:
            self.inhibit_counter -= 1
        else:
            self.filter_disable = False

        if self.filter_disable:
            return sample

        # Adaptive mean filter
        self.sum_val = self.sum_val - self.buffer[self.buffer_index] + sample
        self.buffer[self.buffer_index] = sample
        out = self.sum_val / self.window_size
        self.buffer_index = (self.buffer_index + 1) % self.window_size
        return out

    def _update_window_stats_fast(self, sample: float) -> None:
        if len(self.max_window) == self.max_window_size:
            old = self.max_window.popleft()
            self.max_window_sum -= old
            if self.max_window_max and old == self.max_window_max[0]:
                self.max_window_max.popleft()
            if self.max_window_min and old == self.max_window_min[0]:
                self.max_window_min.popleft()

        self.max_window.append(sample)
        self.max_window_sum += sample

        while self.max_window_max and self.max_window_max[-1] < sample:
            self.max_window_max.pop()
        self.max_window_max.append(sample)

        while self.max_window_min and self.max_window_min[-1] > sample:
            self.max_window_min.pop()
        self.max_window_min.append(sample)

    def consume_new_bpm(self) -> tuple[int, int] | None:
        """Return (BPM, polarity) if a new beat was detected since last call."""
        if self.newBPM:
            self.newBPM = False
            self.last_bpm = self.BPM
            return self.BPM, self.last_r_polarity
        return None

    def last(self, n: int) -> tuple[list[float], list[float]]:
        """Return last n samples/timestamps as Python lists."""
        if n <= 0 or self.filled == 0:
            return [], []
        n = min(n, self.filled)
        start = (self.write_idx - n) % self.length
        end = self.write_idx
        if start < end:
            samples = self.samples[start:end]
            timestamps = self.timestamps[start:end]
        else:
            samples = self.samples[start:] + self.samples[:end]
            timestamps = self.timestamps[start:] + self.timestamps[:end]
        return samples.copy(), timestamps.copy()
