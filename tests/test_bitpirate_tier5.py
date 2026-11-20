"""Tier 5 tests: 1-Wire DS18B20 lifecycle (ping / temp / scratchpad write).

Transcript-driven against the v1.8 firmware source:
- OneWireController.cpp handlePing ("OneWire Ping: Device present.")
- OneWireController.cpp handleTemperature (ROM search, 0x44 convert,
  0xBE 9-byte scratchpad, CRC, "Temperature: 25.00 °C")
- OneWireController.cpp handleWrite/handleScratchpadWrite ("write sp",
  "OneWire Write: Scratchpad write successful.")
Live-verified values (2026-11-19 rig): ROM 28 11 22 33 44 55 66 56,
spad 90 01 0F FF FF FF 00 00 -> 25.00 deg C (raw 0x0190 / 16).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from buspirate_mcp.bitpirate_hardware import BitPirateCLIError, BitPirateHardware
from buspirate_mcp.session import SessionManager
from buspirate_mcp.tools import (
    tool_onewire_ping,
    tool_onewire_temp,
    tool_onewire_write_spad,
)
from test_bitpirate_hardware import FakeSerial

PROMPT_OW = "1WIRE> "

SPAD_25C = bytes.fromhex("90010FFFFFFF0202")
SPAD_25C_HEX = "90010FFFFFFF0202"


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
    h.onewire_pin = 9
    return h, fake


# --- driver: onewire_temp ---


def test_onewire_temp_parses(hw):
    h, fake = hw
    h._active_mode = "1wire"
    fake.expect(
        "temp",
        "OneWire Temp: Searching for DS18B20...\n"
        "DS18B20 ROM: 28 11 22 33 44 55 66 56 \n"
        "Temperature: 25.00 \u00b0C\r\n" + PROMPT_OW,
    )
    result = h.onewire_temp()
    assert result == {"rom": "2811223344556656", "temp_c": 25.0}


def test_onewire_temp_negative(hw):
    h, fake = hw
    h._active_mode = "1wire"
    fake.expect(
        "temp",
        "OneWire Temp: Searching for DS18B20...\n"
        "DS18B20 ROM: 28 11 22 33 44 55 66 56 \n"
        "Temperature: -5.50 \u00b0C\r\n" + PROMPT_OW,
    )
    result = h.onewire_temp()
    assert result["temp_c"] == -5.5
    assert result["rom"] == "2811223344556656"


def test_onewire_temp_no_device_raises(hw):
    h, fake = hw
    h._active_mode = "1wire"
    fake.expect(
        "temp",
        "OneWire Temp: Searching for DS18B20...\n"
        "OneWire Temp: No DS18B20 device found.\r\n" + PROMPT_OW,
    )
    with pytest.raises(BitPirateCLIError, match="No DS18B20"):
        h.onewire_temp()


def test_onewire_temp_crc_error_raises(hw):
    h, fake = hw
    h._active_mode = "1wire"
    fake.expect(
        "temp",
        "OneWire Temp: Searching for DS18B20...\n"
        "DS18B20 ROM: 28 11 22 33 44 55 66 56 \n"
        "OneWire Temp: CRC error in scratchpad.\r\n" + PROMPT_OW,
    )
    with pytest.raises(BitPirateCLIError, match="CRC error"):
        h.onewire_temp()


# --- driver: onewire_write_spad ---


def test_onewire_write_spad_success(hw):
    h, fake = hw
    h._active_mode = "1wire"
    fake.expect(
        "write sp 0x90 0x01 0x0F 0xFF 0xFF 0xFF 0x02 0x02",
        "OneWire Write: Waiting for device... Press [ENTER] to stop\r\n"
        "Attempt 1...\r\n"
        "OneWire Write: Scratchpad write successful.\r\n" + PROMPT_OW,
    )
    assert h.onewire_write_spad(SPAD_25C) is True


def test_onewire_write_spad_failure_raises(hw):
    h, fake = hw
    h._active_mode = "1wire"
    fake.expect(
        "write sp 0x90 0x01 0x0F 0xFF 0xFF 0xFF 0x02 0x02",
        "OneWire Write: Waiting for device... Press [ENTER] to stop\r\n"
        "Attempt 8...\r\n"
        "OneWire Write: Failed after 3 attempts.\r\n" + PROMPT_OW,
    )
    with pytest.raises(BitPirateCLIError, match="scratchpad write failed"):
        h.onewire_write_spad(SPAD_25C)


def test_onewire_write_spad_rejects_wrong_length(hw):
    h, _ = hw
    h._active_mode = "1wire"
    with pytest.raises(ValueError, match="exactly 8 bytes"):
        h.onewire_write_spad(b"\x11" * 7)


# --- tool-level (shape/contract) ---


class TestToolOnewireTier5:
    @pytest.mark.asyncio
    async def test_tool_onewire_ping_present(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        hw_mock = MagicMock()
        hw_mock.onewire_reset = MagicMock(return_value=True)
        session = mgr.create(
            name="t", hardware=hw_mock, protocol="1wire", protocol_config={}
        )
        result = await tool_onewire_ping(
            session_manager=mgr, session_id=session.session_id
        )
        assert result == {"present": True}

    @pytest.mark.asyncio
    async def test_tool_onewire_ping_absent(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        hw_mock = MagicMock()
        hw_mock.onewire_reset = MagicMock(return_value=False)
        session = mgr.create(
            name="t", hardware=hw_mock, protocol="1wire", protocol_config={}
        )
        result = await tool_onewire_ping(
            session_manager=mgr, session_id=session.session_id
        )
        assert result == {"present": False}

    @pytest.mark.asyncio
    async def test_tool_onewire_temp_shape(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        hw_mock = MagicMock()
        hw_mock.onewire_temp = MagicMock(
            return_value={"rom": "2811223344556656", "temp_c": 25.0}
        )
        session = mgr.create(
            name="t", hardware=hw_mock, protocol="1wire", protocol_config={}
        )
        result = await tool_onewire_temp(
            session_manager=mgr, session_id=session.session_id
        )
        assert result["temp_c"] == 25.0
        assert result["rom"] == "2811223344556656"

    @pytest.mark.asyncio
    async def test_tool_onewire_write_spad_shape(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        hw_mock = MagicMock()
        hw_mock.onewire_write_spad = MagicMock(return_value=True)
        session = mgr.create(
            name="t", hardware=hw_mock, protocol="1wire", protocol_config={}
        )
        result = await tool_onewire_write_spad(
            session_manager=mgr,
            session_id=session.session_id,
            spad_hex=SPAD_25C_HEX,
        )
        assert result == {"written": True, "spad_hex": SPAD_25C_HEX}
        args, _ = hw_mock.onewire_write_spad.call_args
        assert args[0] == SPAD_25C
