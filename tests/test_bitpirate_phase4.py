"""Phase 4 tests: UART console-backed session driver (last stub family).

Transcript-driven against the A-firmware source:
- UartController.cpp (7-prompt config walk, handleWrite hex{...}, uread)
- UartService.cpp (configure -> _serial->end(): reconfigure flushes RX)
- TerminalCommandTransformer.cpp (autoCorrectRoot: exact autoCompleteWords
  entries skip 1-edit-distance correction -> "uread" is registered)
Live-verified (2026-11-18 rig): uread idle -> "UART READ: none";
after PING -> 10 x "PONG\\r\\n" plus residual ECHO lines.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from buspirate_mcp.bitpirate_hardware import BitPirateCLIError, BitPirateHardware
from buspirate_mcp.session import SessionManager
from buspirate_mcp.tools import (
    tool_close_uart,
    tool_open_uart,
    tool_read_output,
    tool_scan_baud,
    tool_send_command,
)
from test_bitpirate_hardware import FakeSerial

PROMPT_UART = "UART> "

# Full 7-prompt walk transcript (prompt text per UartController::handleConfig).
WALK = [
    ("mode uart", "UART Configuration:\nRX GPIO [44]: "),
    ("44", "TX GPIO [43]: "),
    ("43", "Baud rate [115200]: "),
    ("115200", "Data bits (5-8) [8]: "),
    ("8", "Parity (N/E/O) [N]: "),
    ("N", "Stop bits (1 or 2) [1]: "),
    ("1", "Inverted? [n]: "),
    ("n", "UART configured.\r\n" + PROMPT_UART),
]


def queue_walk(fake: FakeSerial, baud: int = 115200, first: str = "mode uart") -> None:
    """Queue a full config-walk transcript (first line is mode entry or config)."""
    fake.expect(first, "UART Configuration:\nRX GPIO [44]: ")
    fake.expect("44", "TX GPIO [43]: ")
    fake.expect("43", "Baud rate [115200]: ")
    fake.expect(str(baud), "Data bits (5-8) [8]: ")
    fake.expect("8", "Parity (N/E/O) [N]: ")
    fake.expect("N", "Stop bits (1 or 2) [1]: ")
    fake.expect("1", "Inverted? [n]: ")
    fake.expect("n", "UART configured.\r\n" + PROMPT_UART)


@pytest.fixture
def hw() -> tuple[BitPirateHardware, FakeSerial]:
    fake = FakeSerial()
    h = BitPirateHardware.__new__(BitPirateHardware)
    h.port = "/dev/ttyACM1"
    h.baud = 115200
    h.serial = fake  # type: ignore[assignment]
    h._active_mode = None
    h._flash_shell_open = False
    h._last_voltage_mv = 0
    h._last_current_ma = 0
    h.i2c_sda = 5
    h.i2c_scl = 6
    h.i2c_speed = 100000
    h.onewire_pin = 9
    h.uart_rx = 44
    h.uart_tx = 43
    h.uart_baud = 115200
    return h, fake


# --- mode entry / config walk ---


def test_uart_mode_entry_walk(hw):
    h, fake = hw
    queue_walk(fake)
    h.configure_uart(115200)
    assert h._active_mode == "uart"
    sent = [l for l in fake.lines_sent if l]
    assert sent == ["mode uart", "44", "43", "115200", "8", "N", "1", "n"]


def test_uart_mode_entry_no_walk(hw):
    """Re-entering an already-configured mode: walk skipped, prompt only."""
    h, fake = hw
    fake.expect("mode uart", PROMPT_UART)
    h.configure_uart(115200)
    assert h._active_mode == "uart"
    sent = [l for l in fake.lines_sent if l]
    assert sent == ["mode uart"]


def test_uart_reconfigure_baud_via_config(hw):
    h, fake = hw
    h._active_mode = "uart"
    queue_walk(fake, baud=57600, first="config")
    h.configure_uart(57600)
    sent = [l for l in fake.lines_sent if l]
    assert sent == ["config", "44", "43", "57600", "8", "N", "1", "n"]
    assert h.uart_baud == 57600


def test_uart_configure_rejects_bad_args(hw):
    h, _ = hw
    with pytest.raises(ValueError):
        h.configure_uart(50)  # below range
    with pytest.raises(ValueError):
        h.configure_uart(2_000_000)  # above range
    with pytest.raises(ValueError):
        h.configure_uart(115200, parity=True)  # rig is 8N1
    with pytest.raises(ValueError):
        h.configure_uart(115200, stop_bits=2)


# --- write ---


def test_uart_write(hw):
    h, fake = hw
    h._active_mode = "uart"
    fake.expect(
        "write hex{50 49 4E 47 0D 0A}",
        "UART Write: sent 5 bytes at baud 115200\r\n" + PROMPT_UART,
    )
    h.write(b"PING\r\n")
    assert "write hex{50 49 4E 47 0D 0A}" in fake.lines_sent


def test_uart_write_enters_mode_first(hw):
    """write() from outside UART mode does the full walk, then writes."""
    h, fake = hw
    queue_walk(fake)
    fake.expect(
        "write hex{41 0D 0A}",
        "UART Write: sent 2 bytes at baud 115200\r\n" + PROMPT_UART,
    )
    h.write(b"A\r\n")
    sent = [l for l in fake.lines_sent if l]
    assert sent[-1] == "write hex{41 0D 0A}"
    assert sent[0] == "mode uart"


def test_uart_write_validation(hw):
    h, _ = hw
    with pytest.raises(ValueError):
        h.write(b"")
    with pytest.raises(ValueError):
        h.write("PING")  # type: ignore[arg-type]


def test_uart_write_chunks_over_75_bytes(hw):
    """MAX_LINE_CHARS=250 -> one write hex{} per ~75 bytes."""
    h, fake = hw
    h._active_mode = "uart"
    payload = bytes(range(200))  # 200 bytes -> 3 chunks at the 79-byte cap
    lines = []
    chunk = (250 - len("write hex{}")) // 3  # 79 — must match the driver
    for i in range(0, 200, chunk):
        part = payload[i:i + chunk]
        line = "write hex{" + " ".join(f"{b:02X}" for b in part) + "}"
        lines.append(line)
        fake.expect(line, "UART Write: sent N\r\n" + PROMPT_UART)
    h.write(payload)
    sent = [l for l in fake.lines_sent if l and l.startswith("write hex{")]
    assert sent == lines
    assert len(sent) == 3


def test_uart_write_unconfirmed_raises(hw):
    h, fake = hw
    h._active_mode = "uart"
    fake.expect("write hex{50 0D 0A}", "garbage\r\n" + PROMPT_UART)
    with pytest.raises(BitPirateCLIError):
        h.write(b"P\r\n")


# --- read (uread) ---


def test_uart_read_bytes(hw):
    h, fake = hw
    h._active_mode = "uart"
    fake.expect(
        "uread 256 150",
        "UART READ: 504F4E470D0A\r\n" + PROMPT_UART,
    )
    assert h.read() == b"PONG\r\n"


def test_uart_read_none(hw):
    h, fake = hw
    h._active_mode = "uart"
    fake.expect("uread 256 150", "UART READ: none\r\n" + PROMPT_UART)
    assert h.read() == b""


def test_uart_read_parse_error(hw):
    h, fake = hw
    h._active_mode = "uart"
    fake.expect("uread 256 150", "garbage\r\n" + PROMPT_UART)
    with pytest.raises(BitPirateCLIError):
        h.read()


def test_uart_read_does_not_rewalk(hw):
    """read() in an active session sends only uread (no config walk):
    a reconfigure would flush the RX buffer and drop the response."""
    h, fake = hw
    h._active_mode = "uart"
    fake.expect("uread 256 150", "UART READ: 410D0A\r\n" + PROMPT_UART)
    assert h.read() == b"A\r\n"
    sent = [l for l in fake.lines_sent if l]
    assert sent == ["uread 256 150"]


# --- tool-level (shape/contract) ---


class TestToolUart:
    @pytest.mark.asyncio
    async def test_open_uart_shape(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        hw_mock = MagicMock()
        result = await tool_open_uart(
            session_manager=mgr,
            hardware=hw_mock,
            baud=115200,
            pins={"rx": "44", "tx": "43"},
            engagement_name="t",
        )
        hw_mock.configure_uart.assert_called_once_with(speed=115200)
        assert result["baud"] == 115200
        assert result["pins"] == {"rx": "44", "tx": "43"}
        assert result["session_id"]

    @pytest.mark.asyncio
    async def test_send_command_shape(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        hw_mock = MagicMock()
        it = iter([b"PONG\r\n"])
        hw_mock.write = MagicMock()
        hw_mock.read = MagicMock(side_effect=lambda: next(it, b""))
        session = mgr.create(name="t", hardware=hw_mock, protocol="uart",
                             protocol_config={})
        result = await tool_send_command(
            session_manager=mgr, session_id=session.session_id, command="PING"
        )
        hw_mock.write.assert_called_once_with(b"PING\r\n")
        assert "PONG" in result["response"]
        assert result["bytes_received"] == 6

    @pytest.mark.asyncio
    async def test_read_output_idle(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        hw_mock = MagicMock()
        hw_mock.read = MagicMock(return_value=b"")
        session = mgr.create(name="t", hardware=hw_mock, protocol="uart",
                             protocol_config={})
        result = await tool_read_output(
            session_manager=mgr, session_id=session.session_id, timeout_ms=400
        )
        assert result["bytes_received"] == 0
        assert result["text"] == ""

    @pytest.mark.asyncio
    async def test_scan_baud_captures_scores_and_restores(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        hw_mock = MagicMock()
        hw_mock.uart_baud = 115200
        hw_mock.read = MagicMock(side_effect=lambda: b"\x00" * 16)
        result = await tool_scan_baud(hardware=hw_mock)
        # 8 common rates scored
        assert len(result["candidates"]) == 8
        # the last reconfigure restores the pre-scan baud
        speeds = [c.kwargs.get("speed") or c.args[0]
                  for c in hw_mock.configure_uart.call_args_list]
        assert speeds[0] == 115200  # list order starts at 115200
        assert speeds[-1] == 115200  # restored after the loop

    @pytest.mark.asyncio
    async def test_close_uart(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        hw_mock = MagicMock()
        session = mgr.create(name="t", hardware=hw_mock, protocol="uart",
                             protocol_config={})
        result = await tool_close_uart(
            session_manager=mgr, session_id=session.session_id
        )
        assert result["closed"] is True
