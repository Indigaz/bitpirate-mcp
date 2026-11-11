"""Tests for MCP tool implementations."""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from buspirate_mcp.bitpirate_hardware import PSUUnavailableError
from buspirate_mcp.tools import (
    tool_list_devices,
    tool_verify_connection,
    tool_scan_baud,
    tool_open_uart,
    tool_read_output,
    tool_send_command,
    tool_close_uart,
    tool_set_voltage,
    tool_set_power,
    tool_enter_download_mode,
    tool_read_flash,
    tool_open_1wire,
    tool_onewire_search,
    tool_onewire_read,
    tool_close_1wire,
    tool_open_i2c,
    tool_i2c_scan,
    tool_i2c_read,
    tool_i2c_write,
    tool_i2c_dump,
    tool_close_i2c,
    tool_open_spi,
    tool_spi_probe,
    tool_spi_read,
    tool_spi_dump,
    tool_spi_write,
    tool_spi_transfer,
    tool_spi_config,
    tool_spi_flash_analyze,
    tool_spi_flash_search,
    tool_spi_flash_strings,
    tool_spi_flash_erase,
    tool_close_spi,
    _ascii_score,
    _onewire_crc8,
)
from buspirate_mcp.session import SessionManager


class TestAsciiScore:
    def test_empty_data(self):
        assert _ascii_score(b"") == 0.0

    def test_all_printable(self):
        assert _ascii_score(b"Hello World\n") == 1.0

    def test_all_garbage(self):
        assert _ascii_score(bytes(range(128, 160))) < 0.1

    def test_mixed(self):
        data = b"Hello" + bytes(range(128, 133))
        score = _ascii_score(data)
        assert 0.4 < score < 0.6

    def test_tabs_and_newlines_are_printable(self):
        assert _ascii_score(b"\t\n\r") == 1.0


class TestListDevices:
    @pytest.mark.asyncio
    async def test_returns_device_list(self):
        with patch("buspirate_mcp.tools.BitPirateHardware") as mock_hw:
            mock_hw.list_devices.return_value = [
                {"path": "/dev/ttyACM0"},
                {"path": "/dev/ttyACM1"},
            ]
            result = await tool_list_devices()
            assert len(result["devices"]) == 2

    @pytest.mark.asyncio
    async def test_returns_empty_with_hint(self):
        with patch("buspirate_mcp.tools.BitPirateHardware") as mock_hw:
            mock_hw.list_devices.return_value = []
            result = await tool_list_devices()
            assert result["devices"] == []
            assert "hint" in result


class TestVerifyConnection:
    @pytest.mark.asyncio
    async def test_detects_activity(self):
        mock_hw = MagicMock()
        mock_hw.measure_pin.side_effect = [
            {"rising": 10, "falling": 10, "frequency_hz": 10.0},
            {"rising": 0, "falling": 0, "frequency_hz": 0.0},
        ]
        result = await tool_verify_connection(
            hardware=mock_hw, pins={"tx": 4, "rx": 5}, sample_duration_ms=100,
        )
        assert result["activity_detected"] is True
        assert result["transitions"] == 20

    @pytest.mark.asyncio
    async def test_no_activity_on_quiescent_target(self):
        mock_hw = MagicMock()
        mock_hw.measure_pin.return_value = {
            "rising": 0, "falling": 0, "frequency_hz": 0.0,
        }
        result = await tool_verify_connection(
            hardware=mock_hw, pins={"tx": 4, "rx": 5}, sample_duration_ms=100,
        )
        assert result["activity_detected"] is False
        assert result["transitions"] == 0

    @pytest.mark.asyncio
    async def test_caps_sample_duration_at_5000(self):
        mock_hw = MagicMock()
        mock_hw.measure_pin.return_value = {
            "rising": 0, "falling": 0, "frequency_hz": 0.0,
        }
        result = await tool_verify_connection(
            hardware=mock_hw, pins={"tx": 4, "rx": 5}, sample_duration_ms=99999,
        )
        assert result["sample_duration_ms"] == 5000
        for call in mock_hw.measure_pin.call_args_list:
            assert call.args[1] == 5000

    @pytest.mark.asyncio
    async def test_measures_both_pins(self):
        mock_hw = MagicMock()
        mock_hw.measure_pin.return_value = {
            "rising": 1, "falling": 1, "frequency_hz": 1.0,
        }
        await tool_verify_connection(
            hardware=mock_hw, pins={"tx": 4, "rx": 5}, sample_duration_ms=100,
        )
        assert [c.args[0] for c in mock_hw.measure_pin.call_args_list] == [4, 5]


class TestScanBaud:
    @pytest.mark.asyncio
    async def test_finds_correct_baud(self):
        mock_hw = MagicMock()
        last_speed = [0]

        def track_configure(**kwargs):
            last_speed[0] = kwargs.get("speed", 0)

        def fake_read():
            if last_speed[0] == 9600:
                return b"Linux version 5.15.0\nroot@device:~# "
            return bytes(range(128, 160))

        mock_hw.configure_uart = MagicMock(side_effect=track_configure)
        mock_hw.read = MagicMock(side_effect=fake_read)

        result = await tool_scan_baud(hardware=mock_hw)
        assert result["recommended"] == 9600
        assert result["best_score"] >= 0.7
        assert result["candidates"][0]["baud"] == 9600

    @pytest.mark.asyncio
    async def test_reports_failure_when_no_readable_rate(self):
        mock_hw = MagicMock()
        mock_hw.configure_uart = MagicMock()
        mock_hw.read = MagicMock(return_value=bytes(range(128, 160)))

        result = await tool_scan_baud(hardware=mock_hw)
        assert result["best_score"] < 0.7
        assert result["recommended"] is None

    @pytest.mark.asyncio
    async def test_handles_none_reads(self):
        mock_hw = MagicMock()
        mock_hw.configure_uart = MagicMock()
        mock_hw.read = MagicMock(return_value=None)

        result = await tool_scan_baud(hardware=mock_hw)
        assert result["best_score"] == 0.0
        for c in result["candidates"]:
            assert c["sample_bytes"] == 0


class TestOpenUart:
    @pytest.mark.asyncio
    async def test_creates_session(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        result = await tool_open_uart(
            session_manager=mgr,
            hardware=mock_hw,
            baud=115200,
            pins={"tx": 4, "rx": 5},
            engagement_name="test-device",
        )
        assert "session_id" in result
        assert "engagement_path" in result

    @pytest.mark.asyncio
    async def test_passes_device_path(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        result = await tool_open_uart(
            session_manager=mgr,
            hardware=mock_hw,
            baud=115200,
            pins={"tx": 4, "rx": 5},
            engagement_name="test-device",
            device_path="/dev/ttyACM1",
        )
        import json
        config = json.loads(
            (tmp_path / result["engagement_path"].split("/")[-1] / "config.json").read_text()
        )
        assert config["device_path"] == "/dev/ttyACM1"


class TestReadOutput:
    @pytest.mark.asyncio
    async def test_reads_buffered_data(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        mock_hw.read.return_value = b"root@device:~# "
        session = mgr.create(
            name="test", hardware=mock_hw, baud=9600,
            pins={"tx": 4, "rx": 5},
        )
        result = await tool_read_output(
            session_manager=mgr, session_id=session.session_id, timeout_ms=100,
        )
        assert "root@device" in result["text"]

    @pytest.mark.asyncio
    async def test_returns_empty_on_timeout(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        mock_hw.read.return_value = b""
        session = mgr.create(
            name="test", hardware=mock_hw, baud=9600,
            pins={"tx": 4, "rx": 5},
        )
        result = await tool_read_output(
            session_manager=mgr, session_id=session.session_id, timeout_ms=100,
        )
        assert result["text"] == ""
        assert result["bytes_received"] == 0

    @pytest.mark.asyncio
    async def test_handles_none_reads(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        mock_hw.read.return_value = None
        session = mgr.create(
            name="test", hardware=mock_hw, baud=9600,
            pins={"tx": 4, "rx": 5},
        )
        result = await tool_read_output(
            session_manager=mgr, session_id=session.session_id, timeout_ms=100,
        )
        assert result["bytes_received"] == 0


class TestSendCommand:
    @pytest.mark.asyncio
    async def test_sends_and_captures_response(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        mock_hw.read.side_effect = [
            b"Linux device 5.15.0\n",
            b"# ",
        ]
        session = mgr.create(
            name="test", hardware=mock_hw, baud=9600,
            pins={"tx": 4, "rx": 5},
        )
        result = await tool_send_command(
            session_manager=mgr,
            session_id=session.session_id,
            command="uname -a",
            timeout_ms=500,
        )
        assert "Linux" in result["response"]
        mock_hw.write.assert_called_once()


class TestCloseUart:
    @pytest.mark.asyncio
    async def test_closes_session(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        session = mgr.create(
            name="test", hardware=mock_hw, baud=9600,
            pins={"tx": 4, "rx": 5},
        )
        sid = session.session_id
        result = await tool_close_uart(session_manager=mgr, session_id=sid)
        assert result["closed"] is True
        with pytest.raises(KeyError):
            mgr.get(sid)


class TestSetVoltage:
    @pytest.mark.asyncio
    async def test_valid_voltage(self):
        mock_hw = MagicMock()
        mock_hw.set_voltage.return_value = True
        result = await tool_set_voltage(
            hardware=mock_hw, voltage_v=3.3, current_limit_ma=300,
        )
        assert result["applied"] is True
        mock_hw.set_voltage.assert_called_once_with(3.3, 300)

    @pytest.mark.asyncio
    async def test_out_of_range_voltage(self):
        mock_hw = MagicMock()
        result = await tool_set_voltage(
            hardware=mock_hw, voltage_v=12.0, current_limit_ma=300,
        )
        assert result["error"] is not None
        mock_hw.set_voltage.assert_not_called()

    @pytest.mark.asyncio
    async def test_psu_unavailable(self):
        mock_hw = MagicMock()
        mock_hw.set_voltage.side_effect = PSUUnavailableError("no PSU")
        result = await tool_set_voltage(
            hardware=mock_hw, voltage_v=3.3, current_limit_ma=300,
        )
        assert result["applied"] is False
        assert result["psu"] == "unavailable"


class TestSetPower:
    @pytest.mark.asyncio
    async def test_enable_power(self):
        mock_hw = MagicMock()
        mock_hw.set_power.return_value = True
        result = await tool_set_power(hardware=mock_hw, enable=True)
        assert result["applied"] is True

    @pytest.mark.asyncio
    async def test_disable_power(self):
        mock_hw = MagicMock()
        mock_hw.set_power.return_value = True
        result = await tool_set_power(hardware=mock_hw, enable=False)
        assert result["applied"] is True
        assert result["power"] == "off"

    @pytest.mark.asyncio
    async def test_psu_unavailable(self):
        mock_hw = MagicMock()
        mock_hw.set_power.side_effect = PSUUnavailableError("no PSU")
        result = await tool_set_power(hardware=mock_hw, enable=True)
        assert result["applied"] is False
        assert result["psu"] == "unavailable"


class TestEnterDownloadMode:
    @pytest.mark.asyncio
    async def test_toggles_pins(self):
        mock_hw = MagicMock()
        result = await tool_enter_download_mode(
            hardware=mock_hw, boot_pin=0, reset_pin=1,
        )
        assert result["status"] == "download_mode_entered"
        # Verify pin toggle sequence: boot LOW, reset LOW, reset HIGH, release both
        mock_hw.set_pin_output.assert_any_call(0, high=False)
        mock_hw.set_pin_output.assert_any_call(1, high=False)
        mock_hw.set_pin_output.assert_any_call(1, high=True)
        mock_hw.release_pin.assert_any_call(0)
        mock_hw.release_pin.assert_any_call(1)


class TestReadFlash:
    @pytest.mark.asyncio
    async def test_returns_error_when_esptool_missing(self, tmp_path):
        mock_hw = MagicMock()
        output = tmp_path / "dump.bin"
        with patch("buspirate_mcp.tools._enter_bridge_mode", return_value=True), \
             patch("buspirate_mcp.tools.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("esptool not found")
            result = await tool_read_flash(
                hardware=mock_hw,
                terminal_port="/dev/ttyACM0",
                offset=0,
                size=4096,
                output_path=str(output),
            )
            assert "error" in result

    @pytest.mark.asyncio
    async def test_proceeds_even_if_bridge_entry_returns_false(self, tmp_path):
        """Bridge may already be active from a previous call."""
        mock_hw = MagicMock()
        output = tmp_path / "dump.bin"
        output.write_bytes(b'\xff' * 4096)
        with patch("buspirate_mcp.tools._enter_bridge_mode", return_value=False), \
             patch("buspirate_mcp.tools.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="Read 4096 bytes", stderr="",
            )
            result = await tool_read_flash(
                hardware=mock_hw,
                terminal_port="/dev/ttyACM0",
                offset=0,
                size=4096,
                output_path=str(output),
            )
            assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_success_returns_output_path(self, tmp_path):
        mock_hw = MagicMock()
        output = tmp_path / "subdir" / "dump.bin"
        with patch("buspirate_mcp.tools._enter_bridge_mode", return_value=True), \
             patch("buspirate_mcp.tools.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="Read 4096 bytes", stderr="",
            )
            result = await tool_read_flash(
                hardware=mock_hw,
                terminal_port="/dev/ttyACM0",
                offset=0,
                size=4096,
                output_path=str(output),
            )
            assert result["status"] == "success"
            # Path traversal prevention: directory components stripped
            assert Path(result["output_path"]).name == "dump.bin"


class TestOpen1Wire:
    @pytest.mark.asyncio
    async def test_creates_session_with_1wire_protocol(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        result = await tool_open_1wire(
            session_manager=mgr,
            hardware=mock_hw,
            engagement_name="test-sensor",
        )
        assert "session_id" in result
        assert "engagement_path" in result
        mock_hw.configure_1wire.assert_called_once_with(None, None)

    @pytest.mark.asyncio
    async def test_passes_voltage_and_current(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        await tool_open_1wire(
            session_manager=mgr,
            hardware=mock_hw,
            engagement_name="test-sensor",
            voltage_mv=3300,
            current_ma=200,
        )
        mock_hw.configure_1wire.assert_called_once_with(3300, 200)

    @pytest.mark.asyncio
    async def test_session_protocol_is_1wire(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        result = await tool_open_1wire(
            session_manager=mgr,
            hardware=mock_hw,
            engagement_name="test-sensor",
        )
        session = mgr.get(result["session_id"])
        assert session.protocol == "1wire"


class TestOneWireSearch:
    @pytest.mark.asyncio
    async def test_finds_ds18b20(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        # DS18B20: family=0x28, serial=AA BB CC DD EE FF, crc over first 7 bytes
        rom_without_crc = [0x28, 0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF]
        crc = _onewire_crc8(bytes(rom_without_crc))
        rom_bytes = rom_without_crc + [crc]

        mock_hw.onewire_reset.return_value = True
        mock_hw.onewire_transfer.return_value = rom_bytes

        session = mgr.create(
            name="test", hardware=mock_hw, protocol="1wire",
        )
        result = await tool_onewire_search(
            session_manager=mgr, session_id=session.session_id,
        )

        assert result["present"] is True
        assert result["family_code"] == "0x28"
        assert result["family_name"] == "DS18B20 (Temperature)"
        assert result["serial"] == "AABBCCDDEEFF"
        assert result["crc_valid"] is True
        assert len(result["rom_code"]) == 16  # 8 bytes = 16 hex chars

    @pytest.mark.asyncio
    async def test_logs_transaction(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        rom_without_crc = [0x28, 0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF]
        crc = _onewire_crc8(bytes(rom_without_crc))
        rom_bytes = rom_without_crc + [crc]

        mock_hw.onewire_reset.return_value = True
        mock_hw.onewire_transfer.return_value = rom_bytes

        session = mgr.create(
            name="test", hardware=mock_hw, protocol="1wire",
        )
        await tool_onewire_search(
            session_manager=mgr, session_id=session.session_id,
        )

        log_file = session.engagement_path / "logs" / "1wire-commands.jsonl"
        assert log_file.exists()
        import json
        lines = [json.loads(l) for l in log_file.read_text().splitlines() if l.strip()]
        assert len(lines) == 1
        assert lines[0]["operation"] == "READ_ROM"

    @pytest.mark.asyncio
    async def test_detects_bad_crc(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        # Intentionally corrupt the CRC byte
        mock_hw.onewire_reset.return_value = True
        mock_hw.onewire_transfer.return_value = [0x28, 0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF, 0x00]

        session = mgr.create(
            name="test", hardware=mock_hw, protocol="1wire",
        )
        result = await tool_onewire_search(
            session_manager=mgr, session_id=session.session_id,
        )
        assert result["present"] is True
        assert result["crc_valid"] is False


class TestOneWireSearchNoDevice:
    @pytest.mark.asyncio
    async def test_no_device_when_reset_returns_false(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        mock_hw.onewire_reset.return_value = False

        session = mgr.create(
            name="test", hardware=mock_hw, protocol="1wire",
        )
        result = await tool_onewire_search(
            session_manager=mgr, session_id=session.session_id,
        )
        assert result == {"present": False}
        mock_hw.onewire_transfer.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_device_when_reset_returns_none(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        mock_hw.onewire_reset.return_value = None

        session = mgr.create(
            name="test", hardware=mock_hw, protocol="1wire",
        )
        result = await tool_onewire_search(
            session_manager=mgr, session_id=session.session_id,
        )
        assert result == {"present": False}
        mock_hw.onewire_transfer.assert_not_called()


class TestOneWireRead:
    @pytest.mark.asyncio
    async def test_converts_hex_and_returns_result(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        mock_hw.onewire_transfer.return_value = [0xAA, 0xBB]

        session = mgr.create(
            name="test", hardware=mock_hw, protocol="1wire",
        )
        result = await tool_onewire_read(
            session_manager=mgr,
            session_id=session.session_id,
            write_hex="CC44",
            read_bytes=2,
        )

        assert result["tx"] == "CC44"
        assert result["rx"] == "AABB"
        assert result["bytes_read"] == 2
        mock_hw.onewire_transfer.assert_called_once_with([0xCC, 0x44], 2)

    @pytest.mark.asyncio
    async def test_logs_transaction(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        mock_hw.onewire_transfer.return_value = [0xAA]

        session = mgr.create(
            name="test", hardware=mock_hw, protocol="1wire",
        )
        await tool_onewire_read(
            session_manager=mgr,
            session_id=session.session_id,
            write_hex="BE",
            read_bytes=1,
        )

        log_file = session.engagement_path / "logs" / "1wire-commands.jsonl"
        assert log_file.exists()
        import json
        lines = [json.loads(l) for l in log_file.read_text().splitlines() if l.strip()]
        assert lines[-1]["operation"] == "TRANSFER"
        assert lines[-1]["tx"] == "BE"
        assert lines[-1]["rx"] == "AA"

    @pytest.mark.asyncio
    async def test_handles_empty_transfer_result(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        mock_hw.onewire_transfer.return_value = None

        session = mgr.create(
            name="test", hardware=mock_hw, protocol="1wire",
        )
        result = await tool_onewire_read(
            session_manager=mgr,
            session_id=session.session_id,
            write_hex="CC",
            read_bytes=0,
        )
        assert result["bytes_read"] == 0
        assert result["rx"] == ""


class TestClose1Wire:
    @pytest.mark.asyncio
    async def test_closes_session_and_resets_mode(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()

        session = mgr.create(
            name="test", hardware=mock_hw, protocol="1wire",
        )
        sid = session.session_id
        result = await tool_close_1wire(
            session_manager=mgr, session_id=sid, hardware=mock_hw,
        )

        assert result == {"closed": True}
        mock_hw.reset_mode.assert_called_once()
        with pytest.raises(KeyError):
            mgr.get(sid)


# ---------------------------------------------------------------------------
# SPI tool tests (ESP32-Bit-Pirate native flash shell)
# ---------------------------------------------------------------------------


class TestOpenSPI:
    @pytest.mark.asyncio
    async def test_creates_session_and_switches_mode(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        mock_hw._active_mode = "hiz"
        result = await tool_open_spi(
            session_manager=mgr, hardware=mock_hw, engagement_name="test",
        )
        assert result["status"] == "open"
        assert result["protocol"] == "spi"
        mock_hw._set_mode.assert_called_once_with("spi")

    @pytest.mark.asyncio
    async def test_no_mode_switch_when_already_spi(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        mock_hw._active_mode = "spi"
        result = await tool_open_spi(
            session_manager=mgr, hardware=mock_hw, engagement_name="test",
        )
        assert result["status"] == "open"
        mock_hw._set_mode.assert_not_called()


class TestSPIProbe:
    @pytest.mark.asyncio
    async def test_returns_probe_dict(self):
        mock_hw = MagicMock()
        mock_hw.spi_flash_probe.return_value = {
            "present": True, "id": "EF 40 18", "manufacturer": "Winbond",
            "model": "W25Q128", "capacity_bytes": 16777216,
        }
        result = await tool_spi_probe(hardware=mock_hw)
        assert result["present"] is True
        assert result["id"] == "EF 40 18"
        assert result["capacity_human"] == "16 MB"


class TestSPIRead:
    @pytest.mark.asyncio
    async def test_returns_hexdump(self):
        mock_hw = MagicMock()
        mock_hw.spi_flash_read.return_value = "000000: 00 11 22\n"
        result = await tool_spi_read(hardware=mock_hw, address=0, length=3)
        assert result["hexdump"] == "000000: 00 11 22\n"
        assert result["address"] == 0
        mock_hw.spi_flash_read.assert_called_once_with(0, 3, timeout=120)


class TestSPIDump:
    @pytest.mark.asyncio
    async def test_dump_writes_file(self):
        mock_hw = MagicMock()
        mock_hw.spi_flash_probe.return_value = {
            "present": True, "capacity_bytes": 4096,
        }
        mock_hw._dump_timeout.return_value = 200.0
        mock_hw.spi_flash_dump_raw.return_value = {
            "path": "/tmp/d.bin", "size": 4096,
        }
        result = await tool_spi_dump(hardware=mock_hw, output_path="/tmp/d.bin")
        assert result["size"] == 4096
        assert result["capacity_bytes"] == 4096

    @pytest.mark.asyncio
    async def test_dump_errors_without_chip(self):
        mock_hw = MagicMock()
        mock_hw.spi_flash_probe.return_value = {
            "present": False, "capacity_bytes": 0,
        }
        result = await tool_spi_dump(hardware=mock_hw, output_path="/tmp/d.bin")
        assert "error" in result
        mock_hw.spi_flash_dump_raw.assert_not_called()


class TestSPIWrite:
    @pytest.mark.asyncio
    async def test_write_from_file(self, tmp_path):
        f = tmp_path / "img.bin"
        f.write_bytes(b"\xDE\xAD\xBE\xEF")
        mock_hw = MagicMock()
        mock_hw.spi_flash_write.return_value = "SPI Flash Write: Complete."
        result = await tool_spi_write(
            hardware=mock_hw, address=0x1000, input_path=str(f),
        )
        assert result["status"] == "written"
        args, kwargs = mock_hw.spi_flash_write.call_args
        assert args[0] == 0x1000
        assert kwargs["data_hex"] == "DEADBEEF"

    @pytest.mark.asyncio
    async def test_write_requires_one_source(self):
        mock_hw = MagicMock()
        result = await tool_spi_write(hardware=mock_hw, address=0)
        assert "error" in result
        mock_hw.spi_flash_write.assert_not_called()


class TestSPITransfer:
    @pytest.mark.asyncio
    async def test_instruction_returns_rx(self):
        mock_hw = MagicMock()
        mock_hw.spi_instruction.return_value = b"\xEF\x40\x18"
        result = await tool_spi_transfer(
            hardware=mock_hw, instruction="[0x9F r:3]",
        )
        assert result["rx_hex"] == "EF4018"
        assert result["bytes_read"] == 3

    @pytest.mark.asyncio
    async def test_write_only_instruction_empty(self):
        mock_hw = MagicMock()
        mock_hw.spi_instruction.return_value = b""
        result = await tool_spi_transfer(hardware=mock_hw, instruction="[0x06]")
        assert result["rx_hex"] == ""
        assert result["bytes_read"] == 0


class TestSPIConfig:
    @pytest.mark.asyncio
    async def test_config_passes_pins(self):
        mock_hw = MagicMock()
        result = await tool_spi_config(
            hardware=mock_hw, mosi=40, miso=39, sclk=41, cs=4, freq_mhz=20,
        )
        assert result["configured"] is True
        mock_hw.spi_config.assert_called_once_with(
            40, 39, 41, 4, 20, wp=-1, hold=-1, timeout=15.0,
        )


class TestSPIFlashAnalyze:
    @pytest.mark.asyncio
    async def test_returns_transcript(self):
        mock_hw = MagicMock()
        mock_hw.spi_flash_analyze.return_value = "  SPI Flash Analyze: Done."
        result = await tool_spi_flash_analyze(hardware=mock_hw)
        assert "Done." in result["transcript"]


class TestSPIFlashSearch:
    @pytest.mark.asyncio
    async def test_returns_matches(self):
        mock_hw = MagicMock()
        mock_hw.spi_flash_search.return_value = {
            "pattern": "abc",
            "matches": [{"address": "0x000010", "context": "x"}],
            "transcript": "t",
        }
        result = await tool_spi_flash_search(hardware=mock_hw, pattern="abc")
        assert result["matches"][0]["address"] == "0x000010"


class TestSPIFlashStrings:
    @pytest.mark.asyncio
    async def test_returns_transcript(self):
        mock_hw = MagicMock()
        mock_hw.spi_flash_strings.return_value = "0x000010: hello\n"
        result = await tool_spi_flash_strings(hardware=mock_hw, min_length=5)
        assert "hello" in result["transcript"]
        mock_hw.spi_flash_strings.assert_called_once_with(5, timeout=600)


class TestSPIFlashErase:
    @pytest.mark.asyncio
    async def test_erase(self):
        mock_hw = MagicMock()
        mock_hw.spi_flash_erase.return_value = "SPI Flash Erase: Complete."
        result = await tool_spi_flash_erase(hardware=mock_hw)
        assert result["status"] == "erased"


class TestCloseSPI:
    @pytest.mark.asyncio
    async def test_close_exits_shell_and_hiz(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        mock_hw._active_mode = "spi"
        session = mgr.create(name="t", hardware=mock_hw, protocol="spi")
        result = await tool_close_spi(
            session_manager=mgr, session_id=session.session_id,
            hardware=mock_hw,
        )
        assert result["closed"] is True
        mock_hw._flash_exit.assert_called_once()
        mock_hw.reset_mode.assert_called_once()


# ---------------------------------------------------------------------------
# I2C tool tests
# ---------------------------------------------------------------------------


class TestOpenI2C:
    @pytest.mark.asyncio
    async def test_creates_session_with_i2c_protocol(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        result = await tool_open_i2c(
            session_manager=mgr,
            hardware=mock_hw,
            engagement_name="test-i2c-device",
        )
        assert "session_id" in result
        assert "engagement_path" in result
        assert "i2c_config" in result
        mock_hw.configure_i2c.assert_called_once()

    @pytest.mark.asyncio
    async def test_default_speed_is_400khz(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        result = await tool_open_i2c(
            session_manager=mgr,
            hardware=mock_hw,
            engagement_name="test-i2c-device",
        )
        assert result["i2c_config"]["speed"] == 400000

    @pytest.mark.asyncio
    async def test_config_written_to_disk(self, tmp_path):
        import json
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        result = await tool_open_i2c(
            session_manager=mgr,
            hardware=mock_hw,
            engagement_name="mydev",
            speed=100000,
            clock_stretch=True,
        )
        eng_path = result["engagement_path"]
        config = json.loads((tmp_path / eng_path.split("/")[-1] / "config.json").read_text())
        assert config["protocol"] == "i2c"
        assert config["protocol_config"]["speed"] == 100000
        assert config["protocol_config"]["clock_stretch"] is True


class TestI2CScan:
    @pytest.mark.asyncio
    async def test_deduplicates_read_write_addresses(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        # 0xA0 (write) and 0xA1 (read) both shift to 0x50; same for 0xD0/0xD1 -> 0x68
        mock_hw.i2c_scan.return_value = [0xA0, 0xA1, 0xD0, 0xD1]
        session = mgr.create(
            name="test", hardware=mock_hw, protocol="i2c",
        )
        result = await tool_i2c_scan(session_manager=mgr, session_id=session.session_id)
        assert result["count"] == 2
        addrs = [d["address_7bit"] for d in result["devices"]]
        assert 0x50 in addrs
        assert 0x68 in addrs

    @pytest.mark.asyncio
    async def test_hints_are_populated(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        mock_hw.i2c_scan.return_value = [0xA0, 0xA1, 0xD0, 0xD1]
        session = mgr.create(
            name="test", hardware=mock_hw, protocol="i2c",
        )
        result = await tool_i2c_scan(session_manager=mgr, session_id=session.session_id)
        hints = {d["address_7bit"]: d["hint"] for d in result["devices"]}
        assert hints[0x50] == "EEPROM (24Cxx series)"
        assert hints[0x68] == "RTC/IMU (DS3231, MPU6050)"

    @pytest.mark.asyncio
    async def test_unknown_address_hint_is_none(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        # 0x10 >> 1 = 0x08, not in any known range
        mock_hw.i2c_scan.return_value = [0x10]
        session = mgr.create(
            name="test", hardware=mock_hw, protocol="i2c",
        )
        result = await tool_i2c_scan(session_manager=mgr, session_id=session.session_id)
        assert result["devices"][0]["hint"] is None

    @pytest.mark.asyncio
    async def test_empty_bus(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        mock_hw.i2c_scan.return_value = []
        session = mgr.create(
            name="test", hardware=mock_hw, protocol="i2c",
        )
        result = await tool_i2c_scan(session_manager=mgr, session_id=session.session_id)
        assert result["count"] == 0
        assert result["devices"] == []

    @pytest.mark.asyncio
    async def test_scan_calls_full_range(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        mock_hw.i2c_scan.return_value = []
        session = mgr.create(
            name="test", hardware=mock_hw, protocol="i2c",
        )
        await tool_i2c_scan(session_manager=mgr, session_id=session.session_id)
        mock_hw.i2c_scan.assert_called_once_with(0x00, 0x7F)


class TestI2CRead:
    @pytest.mark.asyncio
    async def test_correct_address_shifting(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        mock_hw.i2c_transfer.return_value = bytes([0x42])
        session = mgr.create(
            name="test", hardware=mock_hw, protocol="i2c",
        )
        await tool_i2c_read(
            session_manager=mgr,
            session_id=session.session_id,
            device_addr="0x50",
            register_addr=0x00,
            length=1,
        )
        # 0x50 << 1 = 0xA0, write address
        call_args = mock_hw.i2c_transfer.call_args
        write_data = call_args[0][0]
        assert write_data[0] == 0xA0
        assert write_data[1] == 0x00

    @pytest.mark.asyncio
    async def test_returns_data_correctly(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        mock_hw.i2c_transfer.return_value = bytes([0xDE, 0xAD])
        session = mgr.create(
            name="test", hardware=mock_hw, protocol="i2c",
        )
        result = await tool_i2c_read(
            session_manager=mgr,
            session_id=session.session_id,
            device_addr=0x50,
            register_addr=0x10,
            length=2,
        )
        assert result["data_bytes"] == [0xDE, 0xAD]
        assert result["data_hex"] == "dead"
        assert result["length"] == 2
        assert result["address"] == "0x50"
        assert result["register"] == "0x10"

    @pytest.mark.asyncio
    async def test_no_register_addr(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        mock_hw.i2c_transfer.return_value = bytes([0xFF])
        session = mgr.create(
            name="test", hardware=mock_hw, protocol="i2c",
        )
        result = await tool_i2c_read(
            session_manager=mgr,
            session_id=session.session_id,
            device_addr="0x50",
        )
        call_args = mock_hw.i2c_transfer.call_args
        write_data = call_args[0][0]
        # Only addr_write byte, no register
        assert len(write_data) == 1
        assert result["register"] is None

    @pytest.mark.asyncio
    async def test_int_device_addr(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        mock_hw.i2c_transfer.return_value = bytes([0x00])
        session = mgr.create(
            name="test", hardware=mock_hw, protocol="i2c",
        )
        result = await tool_i2c_read(
            session_manager=mgr,
            session_id=session.session_id,
            device_addr=0x68,
            register_addr=0x00,
        )
        assert result["address"] == "0x68"


class TestI2CWrite:
    @pytest.mark.asyncio
    async def test_writes_correct_bytes(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        mock_hw.i2c_transfer.return_value = b""
        session = mgr.create(
            name="test", hardware=mock_hw, protocol="i2c",
        )
        await tool_i2c_write(
            session_manager=mgr,
            session_id=session.session_id,
            device_addr="0x50",
            register_addr=0x00,
            data_hex="deadbeef",
        )
        call_args = mock_hw.i2c_transfer.call_args
        write_data = call_args[0][0]
        # [addr_write=0xA0, reg=0x00, 0xDE, 0xAD, 0xBE, 0xEF]
        assert write_data == [0xA0, 0x00, 0xDE, 0xAD, 0xBE, 0xEF]
        assert call_args[1]["read_bytes"] == 0

    @pytest.mark.asyncio
    async def test_returns_written_true(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        mock_hw.i2c_transfer.return_value = b""
        session = mgr.create(
            name="test", hardware=mock_hw, protocol="i2c",
        )
        result = await tool_i2c_write(
            session_manager=mgr,
            session_id=session.session_id,
            device_addr="0x50",
            register_addr=0x10,
            data_hex="ff",
        )
        assert result["written"] is True
        assert result["bytes_written"] == 1
        assert result["address"] == "0x50"
        assert result["register"] == "0x10"


class TestI2CDump:
    @pytest.mark.asyncio
    async def test_saves_dump_file(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        # Return 32 bytes of data for each chunk
        mock_hw.i2c_transfer.return_value = bytes(range(32))
        session = mgr.create(
            name="test", hardware=mock_hw, protocol="i2c",
        )
        result = await tool_i2c_dump(
            session_manager=mgr,
            session_id=session.session_id,
            device_addr="0x50",
            size=64,
        )
        assert result["bytes_read"] == 64
        from pathlib import Path
        dump_path = Path(result["file_path"])
        assert dump_path.exists()
        assert dump_path.name == "i2c_dump_50.bin"
        assert dump_path.stat().st_size == 64

    @pytest.mark.asyncio
    async def test_reads_in_chunks(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        mock_hw.i2c_transfer.return_value = bytes(32)
        session = mgr.create(
            name="test", hardware=mock_hw, protocol="i2c",
        )
        await tool_i2c_dump(
            session_manager=mgr,
            session_id=session.session_id,
            device_addr="0x50",
            size=64,
        )
        # 64 / 32 = 2 calls
        assert mock_hw.i2c_transfer.call_count == 2

    @pytest.mark.asyncio
    async def test_handles_none_chunk(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        mock_hw.i2c_transfer.return_value = None
        session = mgr.create(
            name="test", hardware=mock_hw, protocol="i2c",
        )
        result = await tool_i2c_dump(
            session_manager=mgr,
            session_id=session.session_id,
            device_addr="0x50",
            size=32,
        )
        # Should fill with 0xff when chunk is None
        assert result["bytes_read"] == 32
        from pathlib import Path
        data = Path(result["file_path"]).read_bytes()
        assert all(b == 0xFF for b in data)

    @pytest.mark.asyncio
    async def test_hex_preview_is_first_64_bytes(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        mock_hw.i2c_transfer.return_value = bytes(range(32))
        session = mgr.create(
            name="test", hardware=mock_hw, protocol="i2c",
        )
        result = await tool_i2c_dump(
            session_manager=mgr,
            session_id=session.session_id,
            device_addr="0x50",
            size=256,
        )
        # Preview should be hex of first 64 bytes = 128 hex chars
        assert len(result["hex_preview"]) == 128

    @pytest.mark.asyncio
    async def test_register_bytes_2_sends_msb_first(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        mock_hw.i2c_transfer.return_value = bytes(32)
        session = mgr.create(
            name="test", hardware=mock_hw, protocol="i2c",
        )
        # Read 64 bytes starting at offset 0x1234 with 16-bit addressing
        await tool_i2c_dump(
            session_manager=mgr,
            session_id=session.session_id,
            device_addr="0x50",
            size=64,
            register_bytes=2,
        )
        # First call should have [addr_write, 0x00, 0x00] for offset 0
        first_call_args = mock_hw.i2c_transfer.call_args_list[0]
        assert first_call_args[0][0] == [0xA0, 0x00, 0x00]
        # Second call at offset 32 should be [addr_write, 0x00, 0x20]
        second_call_args = mock_hw.i2c_transfer.call_args_list[1]
        assert second_call_args[0][0] == [0xA0, 0x00, 0x20]

    @pytest.mark.asyncio
    async def test_register_bytes_invalid_rejected(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        session = mgr.create(
            name="test", hardware=mock_hw, protocol="i2c",
        )
        result = await tool_i2c_dump(
            session_manager=mgr,
            session_id=session.session_id,
            device_addr="0x50",
            size=64,
            register_bytes=4,
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_register_bytes_1_rejects_oversize(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        session = mgr.create(
            name="test", hardware=mock_hw, protocol="i2c",
        )
        # 8-bit addressing can't go beyond 256 bytes
        result = await tool_i2c_dump(
            session_manager=mgr,
            session_id=session.session_id,
            device_addr="0x50",
            size=512,
            register_bytes=1,
        )
        assert "error" in result


class TestCloseI2C:
    @pytest.mark.asyncio
    async def test_closes_session(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        session = mgr.create(
            name="test", hardware=mock_hw, protocol="i2c",
        )
        sid = session.session_id
        result = await tool_close_i2c(
            session_manager=mgr, session_id=sid, hardware=mock_hw,
        )
        assert result["closed"] is True
        with pytest.raises(KeyError):
            mgr.get(sid)

    @pytest.mark.asyncio
    async def test_resets_mode(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        session = mgr.create(
            name="test", hardware=mock_hw, protocol="i2c",
        )
        await tool_close_i2c(
            session_manager=mgr, session_id=session.session_id, hardware=mock_hw,
        )
        mock_hw.reset_mode.assert_called_once()


