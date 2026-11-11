"""Tests for the ESP32-Bit-Pirate text-CLI hardware driver (Phase 1).

Design: /home/christian/.hermes/plans/2026-11-10_183841-bitpirate-mcp-port.md
Section 4. Transcript-driven protocol mock: FakeSerial releases a scripted
response only after the command that triggered it is written (simulating
firmware processing latency), so command/response pairing cannot bleed
across round-trips as a naive queue would allow.

Command/grammar strings must match the v1.8 firmware source
(DioController.cpp, UtilityController.cpp, UserInputManager.cpp).
"""

from __future__ import annotations

import time

import pytest

from buspirate_mcp.bitpirate_hardware import (
    BitPirateCLIError,
    BitPirateHardware,
    MAX_LINE_CHARS,
    PSUUnavailableError,
)


class FakeSerial:
    """Protocol-mock serial port.

    - queue_rx(data): bytes available immediately (e.g. initial prompt).
    - expect(prefix, response): one-shot; when a write() contains `prefix`,
      the response is released after RESPONSE_DELAY (simulates the ~2 s
      firmware main-loop latency, scaled down for tests).
    - read() returns b"" after READ_IDLE when no data is pending (simulates
      pyserial's blocking timeout so driver loops cannot busy-spin).
    """

    RESPONSE_DELAY = 0.1
    READ_IDLE = 0.05

    def __init__(self) -> None:
        self.tx: list[bytes] = []
        self._rx: list[bytes] = []
        self._responses: dict[str, bytes] = {}
        self._pending_release = False
        self.timeout = 0.2
        self.closed = False

    def queue_rx(self, data: str) -> None:
        self._rx.append(data.encode())

    def expect(self, prefix: str, response: str) -> None:
        self._responses[prefix] = response.encode()

    def write(self, data: bytes) -> int:
        self.tx.append(data)
        text = data.decode("ascii", "replace")
        for prefix in list(self._responses):
            if prefix in text:
                self._rx.append(self._responses.pop(prefix))
                self._pending_release = True
        return len(data)

    def read(self, n: int) -> bytes:
        if self._pending_release:
            time.sleep(self.RESPONSE_DELAY)
            self._pending_release = False
        if not self._rx:
            time.sleep(self.READ_IDLE)
            return b""
        chunk, self._rx[0] = self._rx[0][:n], self._rx[0][n:]
        if not self._rx[0]:
            self._rx.pop(0)
        return chunk

    def reset_input_buffer(self) -> None:
        self._rx = []

    def close(self) -> None:
        self.closed = True

    @property
    def lines_sent(self) -> list[str]:
        out = []
        for b in self.tx:
            for part in b.split(b"\r"):
                if part:
                    out.append(part.decode("ascii", "replace"))
        return out


@pytest.fixture
def hw() -> tuple[BitPirateHardware, FakeSerial]:
    fake = FakeSerial()
    h = BitPirateHardware.__new__(BitPirateHardware)
    h.port = "/dev/ttyACM0"
    h.baud = 115200
    h.serial = fake  # type: ignore[assignment]
    h._active_mode = None
    h._last_voltage_mv = 0
    h._last_current_ma = 0
    return h, fake


# --- core primitives ---


def test_send_line_rejects_long_lines(hw):
    h, _ = hw
    with pytest.raises(ValueError):
        h._send_line("x" * (MAX_LINE_CHARS + 1))


def test_wake_drains_quiescent(hw):
    h, fake = hw
    t = h._wake()
    assert isinstance(t, bytes)


def test_cmd_returns_transcript_and_stops_at_prompt(hw):
    h, fake = hw
    fake.expect("mode dio", "DIO mode activated.\rDIO> ")
    t = h._cmd("mode dio")
    assert b"DIO mode activated." in t
    assert h._active_mode is None  # _cmd does not track mode; _set_mode does


def test_set_mode_updates_active_mode(hw):
    h, fake = hw
    fake.expect("mode dio", "DIO> ")
    h._set_mode("dio")
    assert h._active_mode == "dio"
    assert "mode dio" in fake.lines_sent


def test_cmd_raises_on_error_marker(hw):
    h, fake = hw
    fake.expect("read 99", "Error: Invalid address.\rDIO> ")
    with pytest.raises(BitPirateCLIError):
        h._cmd("read 99")


def test_cmd_prompt_match_requires_end_of_transcript(hw):
    """A '>' mid-line must not end the read early; only a trailing prompt."""
    h, fake = hw
    fake.expect("mode dio", "Mode changed to DIO (info)\rDIO> ")
    t = h._cmd("mode dio")
    assert t.endswith(b"DIO> ")


# --- DIO / GPIO ---


def test_set_pin_output_sends_mode_then_set_once(hw):
    h, fake = hw
    fake.expect("mode dio", "DIO> ")
    fake.expect("set 5 H", "DIO Set: Pin 5 set to HIGH\rDIO> ")
    h.set_pin_output(5, True)
    sent = [l for l in fake.lines_sent if l]
    assert "mode dio" in sent and "set 5 H" in sent
    # second call: no repeated mode switch
    fake.expect("set 5 L", "DIO Set: Pin 5 set to LOW\rDIO> ")
    h.set_pin_output(5, False)
    sent = [l for l in fake.lines_sent if l]
    assert sent.count("mode dio") == 1
    assert "set 5 L" in sent


def test_read_pin_parses_value(hw):
    h, fake = hw
    fake.expect("mode dio", "DIO> ")
    fake.expect("read 42", "GPIO 42 = 1 (HIGH)\rDIO> ")
    assert h.read_pin(42) == 1
    fake.expect("read 42", "GPIO 42 = 0 (LOW)\rDIO> ")
    assert h.read_pin(42) == 0


def test_configure_pin_input_and_release(hw):
    h, fake = hw
    fake.expect("mode dio", "DIO> ")
    fake.expect("set 8 I", "DIO Set: GPIO 8 set to INPUT\rDIO> ")
    h.configure_pin_input(8)
    assert "set 8 I" in fake.lines_sent
    fake.expect("set 8 I", "DIO Set: GPIO 8 set to INPUT\rDIO> ")
    h.release_pin(8)
    assert fake.lines_sent.count("set 8 I") == 2


def test_pulse_pin(hw):
    h, fake = hw
    fake.expect("mode dio", "DIO> ")
    fake.expect("pulse 42 1000", "DIO Pulse: done\rDIO> ")
    h.pulse_pin(42, 1000)
    assert "pulse 42 1000" in fake.lines_sent


def test_measure_pin_parses_edges_and_freq(hw):
    h, fake = hw
    fake.expect("mode dio", "DIO> ")
    fake.expect(
        "measure 4 2000",
        "DIO Measure: Sampling GPIO 4 for 2000 ms...\r\n\r\n"
        " Results:\r\n  \u2022 Rising edges:     120\r\n"
        "  \u2022 Falling edges:    119\r\n"
        "  \u2022 Approx. frequency: 119.50 Hz\r\nDIO> ",
    )
    r = h.measure_pin(4, 2000)
    assert r == {"rising": 120, "falling": 119, "frequency_hz": 119.5}


def test_measure_pin_caps_duration_at_5000(hw):
    h, fake = hw
    fake.expect("mode dio", "DIO> ")
    fake.expect(
        "measure 4 5000",
        "DIO Measure: Sampling GPIO 4 for 5000 ms...\r\n"
        " Results:\r\n  \u2022 Rising edges:     0\r\n"
        "  \u2022 Falling edges:    0\r\n"
        "  \u2022 Approx. frequency: 0.00 Hz\r\nDIO> ",
    )
    h.measure_pin(4, 99999)
    assert "measure 4 5000" in fake.lines_sent


def test_validate_pin_rules(hw):
    h, _ = hw
    # free pins (firmware protected list: 3,19,20,22-37,45,46)
    for ok in (0, 1, 5, 8, 15, 18, 21, 38, 39, 40, 42, 47, 48):
        h._validate_pin(ok)
    for bad in (22, 25, 31, 45, 46, 19, 20, 3, -1, 99):
        with pytest.raises(ValueError):
            h._validate_pin(bad)


def test_get_pin_voltages_returns_none(hw):
    h, _ = hw
    assert h.get_pin_voltages() is None


# --- PSU (absent on this board) ---


def test_set_voltage_raises_psu_unavailable(hw):
    h, _ = hw
    with pytest.raises(PSUUnavailableError):
        h.set_voltage(3.3, 100)


def test_set_power_raises_psu_unavailable(hw):
    h, _ = hw
    with pytest.raises(PSUUnavailableError):
        h.set_power(True)


# --- phase stubs ---


@pytest.mark.parametrize(
    "call",
    [
        lambda h: h.configure_uart(speed=115200),
        lambda h: h.read(),
        lambda h: h.write(b"abc"),
        lambda h: h.configure_spi(speed=1000000),
        lambda h: h.spi_transfer(b"\x9f", 3),
        lambda h: h.spi_select(),
        lambda h: h.spi_deselect(),
        lambda h: h.configure_i2c(speed=400000),
        lambda h: h.i2c_transfer(b"\x50", 1),
        lambda h: h.i2c_scan(),
        lambda h: h.configure_1wire(),
        lambda h: h.onewire_reset(),
        lambda h: h.onewire_transfer(b"\xfc", 0),
    ],
)
def test_phase_stubs_raise_not_implemented(hw, call):
    h, _ = hw
    with pytest.raises(NotImplementedError):
        call(h)


# --- lifecycle / discovery ---


def test_list_devices_shape():
    devs = BitPirateHardware.list_devices()
    assert isinstance(devs, list)
    for d in devs:
        assert set(d.keys()) == {"path", "role"}
        assert d["role"] == "console"


def test_find_terminal_port_prefers_first_acm(monkeypatch):
    import buspirate_mcp.bitpirate_hardware as mod
    monkeypatch.setattr(mod, "glob", lambda p: ["/dev/ttyACM0", "/dev/ttyACM1"])
    assert BitPirateHardware.find_terminal_port() == "/dev/ttyACM0"


def test_disconnect_closes_serial(hw):
    h, fake = hw
    fake.expect("mode hiz", "HIZ> ")
    h.disconnect()
    assert fake.closed
    assert h.serial is None
    assert h._active_mode is None
