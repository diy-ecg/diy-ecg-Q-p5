"""
RPC transport and frame parser for the Arduino UNO Q Bridge endpoint.

The STM32 sketch exposes `ecg_get_frame` via Bridge/RPC, returning a MsgPack
binary payload:
[uint8 count][uint32 t0_ms][count * (uint16 sample + uint8 dt_ms)]
Optional leading 0x21 ('!') represents an overflow marker and is ignored.
"""
from __future__ import annotations

import struct

try:
    from arduino.app_utils import Bridge
except ImportError as exc:  # pragma: no cover - only relevant on non-UNO-Q hosts
    Bridge = None
    _bridge_import_error = exc
else:
    _bridge_import_error = None

MAX_SAMPLES = 255


def parse_frame(resp: object) -> tuple[list[int], list[int]]:
    """Decode a binary frame into (samples, timestamps), dropping malformed frames."""
    if not resp:
        return [], []

    buf = bytes(resp)
    if buf[0] == 0x21 and len(buf) > 1:
        buf = buf[1:]

    if len(buf) < 1 + 4:
        return [], []

    count = buf[0]
    if count == 0 or count > MAX_SAMPLES:
        return [], []

    expected = 1 + 4 + count * 3
    if len(buf) != expected:
        return [], []

    offset = 1
    t0 = struct.unpack_from("<I", buf, offset)[0]
    offset += 4

    samples: list[int] = []
    timestamps: list[int] = []
    prev_t = t0
    for _ in range(count):
        sample = struct.unpack_from("<H", buf, offset)[0]
        offset += 2
        dt = buf[offset]
        offset += 1
        ts = prev_t + dt
        samples.append(sample)
        timestamps.append(ts)
        prev_t = ts
    return samples, timestamps


class ArduinoQRpcClient:
    """Lightweight RPC client that mirrors the SerialPort API used by the viewer."""

    def __init__(self) -> None:
        if Bridge is None:
            raise ImportError(
                "arduino.app_utils.Bridge is not available; run on the UNO Q Linux side."
            ) from _bridge_import_error

    def request_frame(self) -> tuple[list[int], list[int]]:
        """Call the MCU RPC endpoint and parse the returned frame."""
        resp = Bridge.call("ecg_get_frame")
        if resp is None:
            return [], []
        return parse_frame(resp)

    def close(self) -> None:
        """Provided for API symmetry with SerialPort; nothing to close here."""
        return


def open_rpc() -> ArduinoQRpcClient:
    """Factory mirroring open_serial() from the legacy transport."""
    return ArduinoQRpcClient()
