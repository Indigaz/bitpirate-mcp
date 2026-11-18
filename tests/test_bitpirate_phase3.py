"""Phase 3 tests: I2C + 1-Wire console-backed session driver.

Transcript-driven against the v1.8 firmware source:
- I2cController.cpp (mode-entry config walk, scan, handleInstruction)
- I2cService.cpp executeByteCode (bracket-instruction semantics)
- InstructionTransformer.cpp (token grammar: 0xNN write, r:N read)
- OneWireController.cpp (config walk, ping, xfer)
- SerialTerminalInput.cpp (readChar non-blocking -> scan runs unattended)
Live-verified values (2026-11-17 rig): ROM 28 11 22 33 44 55 66 56
(CRC 0x56), scratchpad 80 00 0F FF FF FF 16 00.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from buspirate_mcp.bitpirate_hardware import BitPirateCLIError, BitPirateHardware
from buspirate_mcp.session import SessionManager
from buspirate_mcp.tools import (
    tool_i2c_read,
    tool_i2c_scan,
    tool_onewire_read,
    tool_onewire_search,
)
from test_bitpirate_hardware import FakeSerial

PROMPT_I2C = "I2C> "
PROMPT_OW = "1WIRE> "


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
    return h, fake


# --- I2C mode entry ---


def test_i2c_mode_entry_walk(hw):
    h, fake = hw
    fake.expect("mode i2c", "I2C Configuration:\nSDA GPIO [5]: ")
    fake.expect("5", "SCL GPIO [6]: ")
    fake.expect("6", "Frequency [400000]: ")
    fake.expect("100000", "I2C configured.\r\n" + PROMPT_I2C)
    h.configure_i2c(100000)
    assert h._active_mode == "i2c"
    sent = [l for l in fake.lines_sent if l]
    assert sent == ["mode i2c", "5", "6", "100000"]


def test_i2c_reconfigure_via_config(hw):
    h, fake = hw
    h._active_mode = "i2c"
    fake.expect("config", "I2C Configuration:\nSDA GPIO [5]: ")
    fake.expect("5", "SCL GPIO [6]: ")
    fake.expect("6", "Frequency [400000]: ")
    fake.expect("400000", "I2C configured.\r\n" + PROMPT_I2C)
    h.configure_i2c(400000)
    sent = [l for l in fake.lines_sent if l]
    assert sent == ["config", "5", "6", "400000"]
    assert h.i2c_speed == 400000


def test_i2c_mode_entry_no_walk(hw):
    """Re-entering an already-configured mode: walk skipped, prompt only."""
    h, fake = hw
    fake.expect("mode i2c", PROMPT_I2C)
    h.configure_i2c(100000)
    assert h._active_mode == "i2c"


def test_i2c_configure_rejects_bad_speed(hw):
    h, _ = hw
    with pytest.raises(ValueError):
        h.configure_i2c(999)
    with pytest.raises(ValueError):
        h.configure_i2c(5_000_000)


# --- I2C transfer (bracket-instruction path) ---


def test_i2c_transfer_read_single(hw):
    h, fake = hw
    h._active_mode = "i2c"
    fake.expect(
        "[0x50 0x03 r:1]",
        "I2C Read:\nA5 \nByteCode Sequence:\n"
        "Start      | data=0 | bits=8 | repeat=1\n" + PROMPT_I2C,
    )
    assert h.i2c_transfer([0xA0, 3], read_bytes=1) == b"\xA5"


def test_i2c_transfer_read_multi(hw):
    h, fake = hw
    h._active_mode = "i2c"
    fake.expect(
        "[0x50 0x03 r:3]",
        "I2C Read:\nA5 A6 A7 \nByteCode Sequence:\n" + PROMPT_I2C,
    )
    assert h.i2c_transfer([0xA0, 3], 3) == b"\xA5\xA6\xA7"
    assert "[0x50 0x03 r:3]" in fake.lines_sent


def test_i2c_transfer_read_nack_empty(hw):
    """NACKed device: no 'I2C Read:' line -> empty result (caller pads)."""
    h, fake = hw
    h._active_mode = "i2c"
    fake.expect(
        "[0x50 0x03 r:1]",
        "ByteCode Sequence:\n"
        "Write      | data=80 | bits=8 | repeat=1\n" + PROMPT_I2C,
    )
    assert h.i2c_transfer([0xA0, 3], 1) == b""


def test_i2c_transfer_write_single(hw):
    h, fake = hw
    h._active_mode = "i2c"
    fake.expect(
        "[0x50 0x03 0xA5]",
        "ByteCode Sequence:\n"
        "Stop       | data=0 | bits=8 | repeat=1\n" + PROMPT_I2C,
    )
    assert h.i2c_transfer([0xA0, 3, 0xA5], 0) == b""
    assert "[0x50 0x03 0xA5]" in fake.lines_sent


def test_i2c_transfer_two_byte_register(hw):
    """16-bit pointer dump chunk: [0x50 0x00 0x05 r:4]."""
    h, fake = hw
    h._active_mode = "i2c"
    fake.expect(
        "[0x50 0x00 0x05 r:4]",
        "I2C Read:\n01 02 03 04 \nByteCode Sequence:\n" + PROMPT_I2C,
    )
    assert h.i2c_transfer([0xA0, 0, 5], 4) == b"\x01\x02\x03\x04"


def test_i2c_transfer_validation(hw):
    h, _ = hw
    with pytest.raises(ValueError):
        h.i2c_transfer([], 1)
    with pytest.raises(ValueError):
        h.i2c_transfer([0xA0, 0x100], 0)
    with pytest.raises(ValueError):
        h.i2c_transfer([0xA0, 3], -1)
    assert h.i2c_transfer([0xA0], 0) == b""  # address only: no-op


def test_i2c_scan_parse_shift(hw):
    h, fake = hw
    h._active_mode = "i2c"
    fake.expect(
        "scan",
        "I2C Scan: Scanning I2C bus... Press [ENTER] to stop.\n\n"
        "Found device at 0x50\nFound device at 0x68\n\n" + PROMPT_I2C,
    )
    assert h.i2c_scan() == [0xA0, 0xD0]
    fake.expect(
        "scan",
        "I2C Scan: Scanning I2C bus... Press [ENTER] to stop.\n\n"
        "Found device at 0x5\n\n" + PROMPT_I2C,
    )
    # sub-0x10 address: firmware prints "0x5" (no leading zero)
    assert h.i2c_scan() == [0x0A]
    fake.expect(
        "scan",
        "I2C Scan: Scanning I2C bus... Press [ENTER] to stop.\n\n"
        "I2C Scan: No I2C devices found.\n\n" + PROMPT_I2C,
    )
    assert h.i2c_scan(0x60, 0x7F) == []


# --- 1-Wire mode entry ---


def test_1wire_config_walk(hw):
    h, fake = hw
    fake.expect(
        "mode 1wire",
        "OneWire Configuration:\nData GPIO [2]: ",
    )
    fake.expect("9", "OneWire configured.\r\n" + PROMPT_OW)
    h.configure_1wire()
    assert h._active_mode == "1wire"
    sent = [l for l in fake.lines_sent if l]
    assert sent == ["mode 1wire", "9"]


def test_1wire_mode_entry_no_walk(hw):
    """After the first entry the firmware skips the walk (no release hook)."""
    h, fake = hw
    fake.expect("mode 1wire", PROMPT_OW)
    h.configure_1wire()
    assert h._active_mode == "1wire"


def test_1wire_reconfigure_via_config(hw):
    h, fake = hw
    h._active_mode = "1wire"
    fake.expect("config", "OneWire Configuration:\nData GPIO [9]: ")
    fake.expect("9", "OneWire configured.\r\n" + PROMPT_OW)
    h.configure_1wire()
    sent = [l for l in fake.lines_sent if l]
    assert sent == ["config", "9"]


# --- 1-Wire ping / xfer ---


def test_1wire_ping_present_absent(hw):
    h, fake = hw
    h._active_mode = "1wire"
    fake.expect("ping", "OneWire Ping: Device present.\r\n" + PROMPT_OW)
    assert h.onewire_reset() is True
    fake.expect("ping", "OneWire Ping: No device found.\r\n" + PROMPT_OW)
    assert h.onewire_reset() is False


def test_1wire_xfer_rom(hw):
    h, fake = hw
    h._active_mode = "1wire"
    fake.expect(
        "xfer 8 0x33",
        "1WIRE XFER: 28 11 22 33 44 55 66 56\r\n" + PROMPT_OW,
    )
    assert h.onewire_transfer([0x33], 8) == bytes.fromhex("2811223344556656")


def test_1wire_xfer_no_device(hw):
    h, fake = hw
    h._active_mode = "1wire"
    fake.expect("xfer 8 0x33", "1WIRE XFER: no device\r\n" + PROMPT_OW)
    assert h.onewire_transfer([0x33], 8) == b""


def test_1wire_xfer_write_only(hw):
    h, fake = hw
    h._active_mode = "1wire"
    fake.expect(
        "xfer 0 0x0F 0x11 0x22 0x33 0x44 0x55 0x66 0x77 0x52",
        "1WIRE XFER: 0 bytes read\r\n" + PROMPT_OW,
    )
    assert (
        h.onewire_transfer([0x0F, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x52], 0)
        == b""
    )


def test_1wire_xfer_validation_and_parse_error(hw):
    h, fake = hw
    with pytest.raises(ValueError):
        h.onewire_transfer([0x300], 1)
    h._active_mode = "1wire"
    fake.expect("xfer 8 0x33", "garbage\r\n" + PROMPT_OW)
    with pytest.raises(BitPirateCLIError):
        h.onewire_transfer([0x33], 8)


# --- tool-level (shape/contract) ---


class TestToolI2C:
    @pytest.mark.asyncio
    async def test_i2c_read_shape(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        hw_mock = MagicMock()
        hw_mock.i2c_transfer = MagicMock(return_value=b"\xA5")
        session = mgr.create(
            name="t", hardware=hw_mock, protocol="i2c", protocol_config={}
        )
        result = await tool_i2c_read(
            session_manager=mgr,
            session_id=session.session_id,
            device_addr="0x50",
            register_addr=3,
            length=1,
        )
        args, _ = hw_mock.i2c_transfer.call_args
        assert args[0] == [0xA0, 3]  # 8-bit write addr + register
        assert _ == 1 or hw_mock.i2c_transfer.call_args[1].get("read_bytes") == 1
        assert result["data_hex"] == "a5"
        assert result["data_bytes"] == [0xA5]
        assert result["length"] == 1
        assert result["address"] == "0x50"

    @pytest.mark.asyncio
    async def test_i2c_scan_dedup_shift(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        hw_mock = MagicMock()
        hw_mock.i2c_scan = MagicMock(return_value=[0xA0, 0xA0, 0xD0])
        session = mgr.create(
            name="t", hardware=hw_mock, protocol="i2c", protocol_config={}
        )
        result = await tool_i2c_scan(
            session_manager=mgr, session_id=session.session_id
        )
        assert [d["address"] for d in result["devices"]] == ["0x50", "0x68"]
        assert result["count"] == 2


class TestToolOneWire:
    @pytest.mark.asyncio
    async def test_onewire_search_rom_crc(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        hw_mock = MagicMock()
        hw_mock.onewire_reset = MagicMock(return_value=True)
        hw_mock.onewire_transfer = MagicMock(
            return_value=bytes.fromhex("2811223344556656")
        )
        session = mgr.create(
            name="t", hardware=hw_mock, protocol="1wire", protocol_config={}
        )
        result = await tool_onewire_search(
            session_manager=mgr, session_id=session.session_id
        )
        assert result["present"] is True
        assert result["crc_valid"] is True
        assert result["family_code"] == "0x28"
        assert result["rom_code"] == "2811223344556656"

    @pytest.mark.asyncio
    async def test_onewire_search_absent(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        hw_mock = MagicMock()
        hw_mock.onewire_reset = MagicMock(return_value=False)
        session = mgr.create(
            name="t", hardware=hw_mock, protocol="1wire", protocol_config={}
        )
        result = await tool_onewire_search(
            session_manager=mgr, session_id=session.session_id
        )
        assert result == {"present": False}

    @pytest.mark.asyncio
    async def test_onewire_read_tx_rx(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        hw_mock = MagicMock()
        hw_mock.onewire_transfer = MagicMock(
            return_value=bytes.fromhex("80000FFFFFFF1600")
        )
        session = mgr.create(
            name="t", hardware=hw_mock, protocol="1wire", protocol_config={}
        )
        result = await tool_onewire_read(
            session_manager=mgr,
            session_id=session.session_id,
            write_hex="AA",
            read_bytes=8,
        )
        assert result["tx"] == "AA"
        assert result["rx"] == "80000FFFFFFF1600"
        assert result["bytes_read"] == 8
