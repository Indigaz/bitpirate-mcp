"""Phase 2 tests: SPI config + SPI flash shell driver.

Transcript-driven against the v1.8 firmware source
(SpiFlashShell.cpp, SpiController.cpp, UserInputManager.cpp).
FakeSerial protocol mock imported from the Phase 1 test module.
"""

from __future__ import annotations

import pytest

from buspirate_mcp.bitpirate_hardware import BitPirateCLIError, BitPirateHardware
from test_bitpirate_hardware import FakeSerial

MENU = (
    "\n=== SPI Flash Shell ===\n"
    "Select a SPI Flash action:\n"
    "  [1] * Probe\n  [2]   Analyze\n  [3]   Search\n  [4]   Strings\n"
    "  [5]   Read\n  [6]   Write\n  [7]   Dump\n  [8]   Dump RAW\n"
    "  [9]   Erase\n  [10]   \U0001F6AA Exit Shell\n"
    "Enter index (default 1): "
)
PROMPT_SPI = "SPI> "


@pytest.fixture
def hw() -> tuple[BitPirateHardware, FakeSerial]:
    fake = FakeSerial()
    h = BitPirateHardware.__new__(BitPirateHardware)
    h.port = "/dev/ttyACM0"
    h.baud = 115200
    h.serial = fake  # type: ignore[assignment]
    h._active_mode = None
    h._flash_shell_open = False
    h._last_voltage_mv = 0
    h._last_current_ma = 0
    return h, fake


def _seed_spi_mode(h, fake) -> None:
    fake.expect("mode spi", PROMPT_SPI)
    h._set_mode("spi")


# --- SPI config ---


def test_spi_config_walks_prompts(hw):
    h, fake = hw
    _seed_spi_mode(h, fake)
    fake.expect("config", "SPI Configuration:\nMOSI GPIO [40]: ")
    fake.expect("40", "MISO GPIO [39]: ")
    fake.expect("39", "SCLK GPIO [41]: ")
    fake.expect("41", "CS GPIO [4]: ")
    fake.expect("4", "Frequency (MHz) [20]: ")
    fake.expect("20", "Use WP or HOLD ? [n]: ")
    fake.expect("n", "SPI configured.\r\n" + PROMPT_SPI)
    t = h.spi_config(40, 39, 41, 4, 20)
    assert b"SPI configured." in t
    sent = [l for l in fake.lines_sent if l]
    assert sent[-6:] == ["40", "39", "41", "4", "20", "n"]


def test_spi_config_with_wp_hold(hw):
    h, fake = hw
    _seed_spi_mode(h, fake)
    fake.expect("config", "SPI Configuration:\nMOSI GPIO [40]: ")
    fake.expect("40", "MISO GPIO [39]: ")
    fake.expect("39", "SCLK GPIO [41]: ")
    fake.expect("41", "CS GPIO [4]: ")
    fake.expect("4", "Frequency (MHz) [20]: ")
    fake.expect("20", "Use WP or HOLD ? [n]: ")
    fake.expect("y", "WP GPIO [-1]: ")
    fake.expect("7", "HOLD GPIO [-1]: ")
    fake.expect("-1", "WP held HIGH; flash status-register bits are unchanged.\n"
                      "HOLD held HIGH (inactive).\nSPI configured.\r\n" + PROMPT_SPI)
    t = h.spi_config(40, 39, 41, 4, 20, wp=7, hold=-1)
    assert b"SPI configured." in t


def test_spi_config_rejects_bad_freq_and_pin(hw):
    h, _ = hw
    with pytest.raises(ValueError):
        h.spi_config(40, 39, 41, 4, 90)
    with pytest.raises(ValueError):
        h.spi_config(22, 39, 41, 4, 20)  # protected pin


# --- flash probe ---


def test_spi_flash_probe_parses(hw):
    h, fake = hw
    _seed_spi_mode(h, fake)
    fake.expect("flash", MENU)
    fake.expect(
        "1",
        "SPI Flash ID: EF 40 18\nManufacturer: Winbond\nModel: W25Q128\n"
        "Capacity: 16777216 bytes\nRead supported: 0x03 with 3-byte addresses.\n"
        "Page programming supported.\n" + MENU,
    )
    r = h.spi_flash_probe()
    assert r["present"] is True
    assert r["id"] == "EF 40 18"
    assert r["manufacturer"] == "Winbond"
    assert r["model"] == "W25Q128"
    assert r["capacity_bytes"] == 16777216
    assert h._flash_shell_open is True


def test_spi_flash_probe_no_chip(hw):
    h, fake = hw
    _seed_spi_mode(h, fake)
    fake.expect("flash", MENU)
    fake.expect(
        "1",
        "SPI Flash ID: FF FF FF\nNo SPI flash detected (bus error or missing chip).\n"
        + MENU,
    )
    r = h.spi_flash_probe()
    assert r["present"] is False
    assert r["id"] == "FF FF FF"


def test_probe_reuses_open_shell(hw):
    h, fake = hw
    _seed_spi_mode(h, fake)
    fake.expect("flash", MENU)
    fake.expect("1", "SPI Flash ID: EF 40 18\nCapacity: 16777216 bytes\n" + MENU)
    h.spi_flash_probe()
    fake.expect("1", "SPI Flash ID: EF 40 18\nCapacity: 16777216 bytes\n" + MENU)
    h.spi_flash_probe()
    sent = [l for l in fake.lines_sent if l]
    assert sent.count("flash") == 1  # second probe did not re-open the shell


# --- analyze / search / strings / read ---


def test_spi_flash_analyze_returns_transcript(hw):
    h, fake = hw
    _seed_spi_mode(h, fake)
    fake.expect("flash", MENU)
    fake.expect(
        "2",
        "\nSPI Flash Analyze: SPI Flash from 0x00000000... Press [ENTER] to stop.\n"
        "  Detected file signatures:\n    0x1000: ELF (little-endian)\n"
        "  Detected sensitive patterns:\n    0x9F00: 'api_key='\n"
        "\n  SPI Flash Analyze: Done.\n\n" + MENU,
    )
    t = h.spi_flash_analyze()
    assert "SPI Flash Analyze: Done." in t
    assert "api_key=" in t


def test_spi_flash_search_prompts_and_parses_matches(hw):
    h, fake = hw
    _seed_spi_mode(h, fake)
    fake.expect("flash", MENU)
    fake.expect("3", MENU + "Enter string search pattern: ")
    fake.expect(
        "ESP32",
        '\nSearching for "ESP32" in SPI flash from 0x000000... Press [ENTER] to stop.\n\n'
        "0x0000A0: xxxx[ESP32]xxxx\n"
        "0x001F30: xxxx[ESP32]xxxx\n"
        "\nSearch complete.\n" + MENU,
    )
    r = h.spi_flash_search("ESP32")
    assert r["pattern"] == "ESP32"
    assert [m["address"] for m in r["matches"]] == ["0x0000A0", "0x001F30"]
    assert "0x0000A0" in r["transcript"]


def test_spi_flash_search_rejects_bad_pattern_length(hw):
    h, _ = hw
    with pytest.raises(ValueError):
        h.spi_flash_search("")
    with pytest.raises(ValueError):
        h.spi_flash_search("x" * 33)


def test_spi_flash_strings_prompts_and_completes(hw):
    h, fake = hw
    _seed_spi_mode(h, fake)
    fake.expect("flash", MENU)
    fake.expect("4", MENU + "Min. length of the strings: [10]: ")
    fake.expect(
        "12",
        "\nSPI Flash: Extracting strings... Press [ENTER] to stop.\n\n"
        "0x000010: bootloader_banner\n0x000040: secret_token_abc\n"
        "\nSPI Flash: String extraction complete.\n\n" + MENU,
    )
    t = h.spi_flash_strings(min_length=12)
    assert "secret_token_abc" in t
    assert "String extraction complete." in t


def test_spi_flash_read_two_prompts(hw):
    h, fake = hw
    _seed_spi_mode(h, fake)
    fake.expect("flash", MENU)
    fake.expect("5", MENU + "Start address (dec or 0x hex) [0]: ")
    fake.expect("0x0", "Number of bytes to read (dec or 0x hex) [10]: ")
    fake.expect(
        "0x10",
        "SPI Flash Read: In progress... Press [ENTER] to stop\n\n"
        "000000: 00 11 22 33 44 55 66 77 88 99 AA BB CC DD EE FF .\n"
        + MENU,
    )
    t = h.spi_flash_read(0, 16)
    assert "000000:" in t


# --- raw dump ---


def test_spi_flash_dump_raw_writes_file(hw, tmp_path):
    h, fake = hw
    _seed_spi_mode(h, fake)
    fake.expect("flash", MENU)
    fake.expect(
        "8",
        "\nSPI Flash: Full dump from 0x000000... Press [ENTER] to stop.\n\n"
        "The raw mode is for python scripting, Continue? [n]: ",
    )
    payload = bytes(range(256)) * 4
    fake.expect(
        "y",
        payload + b"\n" + b"SPI Flash Dump: Done.\n\n" + MENU.encode(),
    )
    out = tmp_path / "fw.bin"
    r = h.spi_flash_dump_raw(str(out), capacity_bytes=len(payload))
    assert r["size"] == len(payload)
    assert out.read_bytes() == payload


def test_spi_flash_dump_raw_size_and_path(hw, tmp_path):
    h, _ = hw
    # timeout formula: 1 MiB -> max(120, 1048576/11520 + 120) ≈ 211 s
    assert h._dump_timeout(1024 * 1024) == pytest.approx(211.1, abs=1.0)
    assert h._dump_timeout(0) == 1800.0


# --- write / erase (confirmation flows) ---


def test_spi_flash_write_hex_flow(hw):
    h, fake = hw
    _seed_spi_mode(h, fake)
    fake.expect("flash", MENU)
    fake.expect("6", MENU + "Start address (dec or 0x hex) [0]: ")
    fake.expect("0x1000", "Write an ASCII string? [y]: ")
    fake.expect("n", "Enter byte values (e.g., 01 A5 FF...) [0x0000]: ")
    fake.expect("DE AD BE EF", "Automatically erase affected blocks and preserve surrounding bytes? (No: program without erase) [y]: ")
    fake.expect("y", "SPI Flash Write: Confirm write operation? [n]: ")
    fake.expect(
        "y",
        "Writing 4 byte(s) at address 0x001000\nSPI Flash Write: Complete.\n\n" + MENU,
    )
    t = h.spi_flash_write(0x1000, data_hex="DEADBE EF".replace(" ", ""))
    assert "SPI Flash Write: Complete." in t


def test_spi_flash_write_requires_one_data_source(hw):
    h, _ = hw
    with pytest.raises(ValueError):
        h.spi_flash_write(0)
    with pytest.raises(ValueError):
        h.spi_flash_write(0, data_hex="00", ascii_string="x")
    with pytest.raises(ValueError):
        h.spi_flash_write(0, data_hex="")


def test_spi_flash_erase_flow(hw):
    h, fake = hw
    _seed_spi_mode(h, fake)
    fake.expect("flash", MENU)
    fake.expect("9", MENU + "SPI Flash Erase: Erase entire flash memory? [n]: ")
    fake.expect(
        "y",
        "Erasing the entire chip and verifying... This can take several minutes.\n"
        "SPI Flash Erase: Complete.\n\n" + MENU,
    )
    t = h.spi_flash_erase()
    assert "SPI Flash Erase: Complete." in t


# --- instruction syntax ---


def test_spi_instruction_reads_bytes(hw):
    h, fake = hw
    _seed_spi_mode(h, fake)
    fake.expect("[0x9F r:3]", "SPI Read:\nEF 40 18 \r\n" + PROMPT_SPI)
    assert h.spi_instruction("[0x9F r:3]") == b"\xEF\x40\x18"


def test_spi_instruction_write_only_returns_empty(hw):
    h, fake = hw
    _seed_spi_mode(h, fake)
    fake.expect("[0x06]", PROMPT_SPI)
    assert h.spi_instruction("[0x06]") == b""


# --- shell lifecycle ---


def test_flash_exit_closes_shell(hw):
    h, fake = hw
    _seed_spi_mode(h, fake)
    fake.expect("flash", MENU)
    fake.expect("10", "Exiting SPI Flash Shell...\r\n" + PROMPT_SPI)
    h._flash_enter()
    assert h._flash_shell_open is True
    h._flash_exit()
    assert h._flash_shell_open is False


def test_wait_marker_timeout_raises(hw):
    h, fake = hw
    with pytest.raises(BitPirateCLIError):
        h._wait_marker(b"NEVER APPEARS", timeout=0.3)
