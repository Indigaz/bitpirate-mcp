"""BusPirate MCP server -- stdio transport.

Registers all tools from tools.py with the MCP SDK and runs the
server. Claude Code spawns this process and communicates over stdin/stdout.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from buspirate_mcp.bitpirate_hardware import BitPirateHardware
from buspirate_mcp.safety import classify_tool, SafetyTier
from buspirate_mcp.session import SessionManager
from buspirate_mcp import tools

logger = logging.getLogger("buspirate-mcp")

# Engagements dir: env var overrides, fallback to package root.
# In standalone mode: defaults to <repo>/engagements/
# When submoduled: parent repo sets PIDEV_ENGAGEMENTS_DIR via .mcp.json env.
_PACKAGE_ROOT = Path(__file__).resolve().parents[2]
ENGAGEMENTS_DIR = Path(
    os.environ.get("PIDEV_ENGAGEMENTS_DIR", str(_PACKAGE_ROOT / "engagements"))
)

app = Server("bitpirate-mcp")
session_manager = SessionManager(engagements_dir=ENGAGEMENTS_DIR)

# Hardware connection -- initialized lazily on first tool call that needs it
_hardware: BitPirateHardware | None = None
_hardware_port: str = ""


def _get_hardware(port: str | None = None) -> BitPirateHardware:
    global _hardware, _hardware_port
    # Detect dead connection (serial set to None by disconnect or stale state)
    if _hardware is not None and _hardware.serial is None:
        _hardware = None
    if _hardware is None:
        if port is None:
            devices = BitPirateHardware.list_devices()
            if not devices:
                raise RuntimeError("No ESP32-Bit-Pirate console port found on USB")
            port = devices[0]["path"]
        _hardware = BitPirateHardware.connect(port)
        _hardware_port = port
    return _hardware


# Duration class convention (consistent across pidev-sec tool MCPs):
#   instant    -- <1 s wall clock, foregroundable always
#   fast       -- 1-10 s, foregroundable
#   slow       -- 10 s-2 min, background-dispatch recommended
#   very-slow  -- >2 min, background-dispatch effectively required

TOOL_DEFINITIONS = [
    Tool(
        name="list_devices",
        description="Find ESP32-Bit-Pirate console ports on USB CDC serial. [read-only] [Duration: instant.]",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="verify_connection",
        description=(
            "Check for signal activity on specified pins. "
            "User must provide pin mapping. "
            "[read-only] [Duration: fast (~2 s -- samples for sample_duration_ms, default 2000).]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "tx_pin": {"type": "integer", "description": "ESP32 GPIO for TX (e.g., 4)"},
                "rx_pin": {"type": "integer", "description": "ESP32 GPIO for RX (e.g., 5)"},
                "sample_duration_ms": {
                    "type": "integer", "default": 2000,
                    "description": "How long to sample for activity (ms)",
                },
            },
            "required": ["tx_pin", "rx_pin"],
        },
    ),
    Tool(
        name="scan_baud",
        description=(
            "Scan common baud rates and score by readable ASCII output. "
            "Returns candidates sorted by confidence. "
            "[read-only] [Duration: slow (~15-60 s -- cycles through common baud rates, "
            "samples each for a short window). Background-dispatch recommended.]"
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    Tool(
        name="open_uart",
        description=(
            "Open a persistent UART connection and start an engagement. "
            "Creates engagement folder and raw logging. "
            "[allowed-write] [Duration: fast (~0.5-2 s -- mode switch + folder creation).]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "baud": {"type": "integer", "description": "Baud rate"},
                "tx_pin": {"type": "integer", "description": "ESP32 GPIO for TX (e.g., 4)", "default": 4},
                "rx_pin": {"type": "integer", "description": "ESP32 GPIO for RX (e.g., 5)", "default": 5},
                "engagement_name": {"type": "string", "description": "Target device name"},
                "engagement_path": {
                    "type": "string",
                    "description": "Path to an engagement folder (from project-mcp). If provided, writes to <engagement_path>/uart/ instead of creating a standalone engagement.",
                },
            },
            "required": ["baud", "engagement_name"],
        },
    ),
    Tool(
        name="read_output",
        description=(
            "Read available data from an open UART session. "
            "Blocks up to timeout. "
            "[read-only] [Duration: fast (~timeout_ms; default 5 s). "
            "Returns earlier if data is available.]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "timeout_ms": {"type": "integer", "default": 5000},
            },
            "required": ["session_id"],
        },
    ),
    Tool(
        name="send_command",
        description=(
            "Send a command over UART and capture the response. "
            "[allowed-write] [Duration: fast (~timeout_ms; default 3 s). "
            "Returns earlier when response is received.]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "command": {"type": "string"},
                "timeout_ms": {"type": "integer", "default": 3000},
            },
            "required": ["session_id", "command"],
        },
    ),
    Tool(
        name="close_uart",
        description="Close a UART session and finalize logs. [allowed-write] [Duration: instant.]",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
            },
            "required": ["session_id"],
        },
    ),
    Tool(
        name="set_voltage",
        description=(
            "Set power supply voltage (0.8-5.0V) and current limit (0-500mA). "
            "DANGEROUS: wrong voltage can damage target hardware. "
            "[approval-write] [Duration: instant.]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "voltage_v": {"type": "number", "minimum": 0.8, "maximum": 5.0},
                "current_limit_ma": {"type": "integer", "minimum": 0, "maximum": 500},
                "_confirmed": {"type": "boolean", "default": False, "description": "Set true to confirm execution"},
            },
            "required": ["voltage_v", "current_limit_ma"],
        },
        annotations={"destructiveHint": True, "readOnlyHint": False},
    ),
    Tool(
        name="set_power",
        description=(
            "Enable or disable the power supply. "
            "DANGEROUS: affects power to target device. "
            "[approval-write] [Duration: instant.]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "enable": {"type": "boolean"},
                "_confirmed": {"type": "boolean", "default": False, "description": "Set true to confirm execution"},
            },
            "required": ["enable"],
        },
        annotations={"destructiveHint": True, "readOnlyHint": False},
    ),
    Tool(
        name="enter_download_mode",
        description=(
            "Put target into UART download mode by toggling GPIO pins. "
            "Holds boot pin LOW, pulses reset pin, releases. "
            "Works for ESP32, ESP8266, and similar chips. "
            "[allowed-write] [Duration: fast (~1-2 s -- GPIO toggle sequence).]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "boot_pin": {
                    "type": "integer",
                    "description": "ESP32 GPIO connected to target boot select (e.g., target GPIO0)",
                },
                "reset_pin": {
                    "type": "integer",
                    "description": "ESP32 GPIO connected to target EN/RST",
                },
            },
            "required": ["boot_pin", "reset_pin"],
        },
    ),
    Tool(
        name="read_flash",
        description=(
            "Read flash memory from target via esptool through the Bit-Pirate UART bridge. "
            "Optionally enters download mode first. Requires esptool installed. "
            "[allowed-write] [Duration: very-slow (~3-5 min for 1-2 MiB at 460800 baud; "
            "larger images proportionally longer). Background-dispatch required. "
            "Bridge mode persists after dump -- press the Bit-Pirate button or USB-replug to exit.]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "offset": {
                    "type": "integer", "default": 0,
                    "description": "Flash offset to start reading from",
                },
                "size": {
                    "type": "integer",
                    "description": "Number of bytes to read (e.g., 0x400000 for 4MB)",
                },
                "output_path": {
                    "type": "string",
                    "description": "Path to save the flash dump",
                },
                "boot_pin": {
                    "type": "integer", "default": -1,
                    "description": "ESP32 GPIO for boot select (-1 to skip download mode entry)",
                },
                "reset_pin": {
                    "type": "integer", "default": -1,
                    "description": "ESP32 GPIO for reset (-1 to skip download mode entry)",
                },
                "baud": {
                    "type": "integer", "default": 115200,
                    "description": "Baud rate for flash read (115200 or 460800 recommended)",
                },
            },
            "required": ["size", "output_path"],
        },
    ),
    # --- SPI tools (ESP32-Bit-Pirate: native flash shell + instruction syntax) ---
    Tool(
        name="open_spi",
        description=(
            "Enter SPI mode on the ESP32-Bit-Pirate and start an engagement. "
            "Pin map comes from NVS (change with spi_config). "
            "[allowed-write] [Duration: fast (~1-2 s -- mode switch).]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "engagement_name": {"type": "string", "description": "Target device name"},
                "engagement_path": {
                    "type": "string",
                    "description": "Path to an engagement folder (optional).",
                },
            },
            "required": ["engagement_name"],
        },
    ),
    Tool(
        name="spi_probe",
        description=(
            "Detect the SPI flash chip via the firmware flash shell. "
            "Returns JEDEC ID, manufacturer, model, capacity, and capability notes. "
            "[read-only] [Duration: fast (~2-5 s).]"
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="spi_read",
        description=(
            "Read a region of SPI flash memory as a hexdump (firmware menu 5). "
            "[read-only] [Duration: fast - slow (serial-bottlenecked ~11.5 KB/s "
            "-- ~8 s per 100 KiB).]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "address": {"type": "integer", "description": "Flash address to start reading from"},
                "length": {"type": "integer", "description": "Number of bytes to read (max 65536)"},
                "timeout": {"type": "integer", "default": 120, "description": "Timeout seconds"},
            },
            "required": ["address", "length"],
        },
    ),
    Tool(
        name="spi_dump",
        description=(
            "Dump the entire SPI flash to a file (firmware raw mode, menu 8). "
            "Serial-bottlenecked (~91 s per MiB at 115200 baud). "
            "[read-only] [Duration: slow - very-slow (4 MiB flash ~ 7 min). "
            "Background-dispatch required.] Returns {path, size}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "output_path": {"type": "string", "description": "Destination file path (e.g. /home/christian/re-lab/<target>/flash.bin)"},
                "timeout": {"type": "integer", "description": "Timeout seconds (auto from capacity if omitted)"},
            },
            "required": ["output_path"],
        },
    ),
    Tool(
        name="spi_write",
        description=(
            "Write data to SPI flash (firmware menu 6): from a file, hex string, "
            "or ASCII string. DANGEROUS: overwrites flash. "
            "[approval-write] [Duration: fast - slow (depends on size).]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "address": {"type": "integer", "description": "Target flash address"},
                "input_path": {"type": "string", "description": "Path to binary file to write"},
                "data_hex": {"type": "string", "description": "Hex string (e.g. 'DEADBEEF')"},
                "ascii_string": {"type": "string", "description": "ASCII string to write"},
                "auto_erase": {"type": "boolean", "default": True, "description": "Automatically erase affected blocks"},
                "_confirmed": {"type": "boolean", "default": False, "description": "Set true to confirm execution"},
            },
            "required": ["address", "_confirmed"],
        },
        annotations={"destructiveHint": True, "readOnlyHint": False},
    ),
    Tool(
        name="spi_transfer",
        description=(
            "Run a BP6-style SPI instruction, e.g. '[0x9F r:3]' (JEDEC read), "
            "'[0x05 r:1]' (status), '[0x06] [0xC7]' (enable + chip erase). "
            "Dispatched to the firmware's instruction parser in SPI mode. "
            "[read-only for read-only instructions] [Duration: instant - fast.]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "instruction": {"type": "string", "description": "Instruction in [0x.. r:N d:ms] syntax"},
                "timeout": {"type": "integer", "default": 15, "description": "Timeout seconds"},
            },
            "required": ["instruction"],
        },
    ),
    Tool(
        name="spi_config",
        description=(
            "Set the SPI pin map (MOSI/MISO/SCLK/CS GPIO, frequency 1-80 MHz, "
            "optional WP/HOLD pins). NVS-persisted by the firmware. "
            "[allowed-write] [Duration: fast (~2-4 s).]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "mosi": {"type": "integer", "description": "MOSI GPIO"},
                "miso": {"type": "integer", "description": "MISO GPIO"},
                "sclk": {"type": "integer", "description": "SCLK GPIO"},
                "cs": {"type": "integer", "description": "CS GPIO"},
                "freq_mhz": {"type": "integer", "description": "SPI clock 1-80 MHz"},
                "wp": {"type": "integer", "default": -1, "description": "WP GPIO or -1"},
                "hold": {"type": "integer", "default": -1, "description": "HOLD GPIO or -1"},
            },
            "required": ["mosi", "miso", "sclk", "cs", "freq_mhz"],
        },
    ),
    Tool(
        name="spi_flash_analyze",
        description=(
            "Run the on-device BinaryAnalyzer over the whole flash: file "
            "signatures, sensitive patterns (keys, hashes, URLs), entropy "
            "summary. [read-only] [Duration: slow - very-slow (full-chip read "
            "~91 s/MiB + analysis). Background-dispatch recommended.]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "timeout": {"type": "integer", "default": 600, "description": "Timeout seconds"},
            },
        },
    ),
    Tool(
        name="spi_flash_search",
        description=(
            "Search the flash for a string pattern (1-32 bytes). Returns match "
            "addresses with printable context. [read-only] [Duration: slow "
            "(full-chip read ~91 s/MiB). Background-dispatch recommended.]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "String to search for (1-32 bytes)"},
                "timeout": {"type": "integer", "default": 600, "description": "Timeout seconds"},
            },
            "required": ["pattern"],
        },
    ),
    Tool(
        name="spi_flash_strings",
        description=(
            "Extract printable strings from the whole flash (min length "
            "parameter). [read-only] [Duration: slow - very-slow (full-chip "
            "read). Background-dispatch recommended.]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "min_length": {"type": "integer", "default": 10, "description": "Minimum string length"},
                "timeout": {"type": "integer", "default": 600, "description": "Timeout seconds"},
            },
        },
    ),
    Tool(
        name="spi_flash_erase",
        description=(
            "Erase the ENTIRE flash chip (firmware menu 9). DANGEROUS: all "
            "contents destroyed. [approval-write] [Duration: slow - very-slow "
            "(seconds to minutes depending on chip).]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "_confirmed": {"type": "boolean", "default": False, "description": "Set true to confirm execution"},
                "timeout": {"type": "integer", "default": 1800, "description": "Timeout seconds"},
            },
            "required": ["_confirmed"],
        },
        annotations={"destructiveHint": True, "readOnlyHint": False},
    ),
    Tool(
        name="close_spi",
        description="Exit the flash shell and reset the ESP32-Bit-Pirate to HiZ mode. [allowed-write] [Duration: instant.]",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
            },
            "required": ["session_id"],
        },
    ),
    # --- I2C tools ---
    Tool(
        name="open_i2c",
        description=(
            "Open an I2C connection and start an engagement. "
            "Configures I2C mode with pullups enabled. "
            "[allowed-write] [Duration: fast (~0.5-2 s -- mode switch + folder creation).]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "engagement_name": {"type": "string", "description": "Target device name"},
                "speed": {"type": "integer", "default": 400000, "description": "I2C clock speed in Hz"},
                "clock_stretch": {"type": "boolean", "default": False, "description": "Enable clock stretching"},
                "voltage_mv": {"type": "integer", "description": "Target voltage in mV (optional, enables PSU)"},
                "current_ma": {"type": "integer", "description": "Current limit in mA"},
                "engagement_path": {
                    "type": "string",
                    "description": "Path to an engagement folder (from project-mcp). If provided, writes to <engagement_path>/i2c/ instead of creating a standalone engagement.",
                },
            },
            "required": ["engagement_name"],
        },
    ),
    Tool(
        name="i2c_scan",
        description=(
            "Scan the I2C bus for devices. Returns 7-bit addresses with device hints. "
            "[read-only] [Duration: slow (~3-10 s -- sweeps all 128 7-bit addresses). "
            "Background-dispatch recommended.]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
            },
            "required": ["session_id"],
        },
    ),
    Tool(
        name="i2c_read",
        description=(
            "Read bytes from an I2C device, optionally from a specific register. "
            "[read-only] [Duration: instant.]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "device_addr": {
                    "type": ["string", "integer"],
                    "description": "7-bit device address (hex string like '0x50' or integer)",
                },
                "register_addr": {"type": "integer", "description": "Register address to read from (optional)"},
                "length": {"type": "integer", "default": 1, "description": "Number of bytes to read"},
            },
            "required": ["session_id", "device_addr"],
        },
    ),
    Tool(
        name="i2c_write",
        description=(
            "Write bytes to an I2C device register. "
            "DANGEROUS: may alter device configuration or EEPROM contents. "
            "[approval-write] [Duration: instant.]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "device_addr": {
                    "type": ["string", "integer"],
                    "description": "7-bit device address (hex string like '0x50' or integer)",
                },
                "register_addr": {"type": "integer", "description": "Register address to write to"},
                "data_hex": {"type": "string", "description": "Hex string of data bytes to write"},
                "_confirmed": {"type": "boolean", "default": False, "description": "Set true to confirm execution"},
            },
            "required": ["session_id", "device_addr", "register_addr", "data_hex"],
        },
        annotations={"destructiveHint": True, "readOnlyHint": False},
    ),
    Tool(
        name="i2c_dump",
        description=(
            "Dump memory from an I2C device by reading all registers sequentially. "
            "Saves to artifacts. "
            "[read-only] [Duration: slow for large sizes (~1-2 min for 64 KB EEPROM). "
            "Fast for small sizes (256 bytes ~2-5 s). Parameterized by size. "
            "Background-dispatch recommended for large dumps.]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "device_addr": {
                    "type": ["string", "integer"],
                    "description": "7-bit device address (hex string like '0x50' or integer)",
                },
                "size": {"type": "integer", "default": 256, "description": "Number of bytes to dump"},
                "register_bytes": {
                    "type": "integer",
                    "enum": [1, 2],
                    "default": 1,
                    "description": (
                        "Register/memory address width. 1 for most small EEPROMs "
                        "and sensors (up to 256 bytes). 2 for 24C32+ and larger "
                        "EEPROMs (up to 64KB)."
                    ),
                },
            },
            "required": ["session_id", "device_addr"],
        },
    ),
    Tool(
        name="close_i2c",
        description="Close an I2C session and reset the ESP32-Bit-Pirate to HiZ mode. [allowed-write] [Duration: instant.]",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
            },
            "required": ["session_id"],
        },
    ),
    # --- 1-Wire tools ---
    Tool(
        name="open_1wire",
        description=(
            "Open a 1-Wire connection and start an engagement. "
            "Configures 1-Wire mode with pullup enabled. "
            "[allowed-write] [Duration: fast (~0.5-2 s -- mode switch + folder creation).]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "engagement_name": {"type": "string", "description": "Target device name"},
                "voltage_mv": {"type": "integer", "description": "Target voltage in mV (optional, enables PSU)"},
                "current_ma": {"type": "integer", "description": "Current limit in mA"},
                "engagement_path": {
                    "type": "string",
                    "description": "Path to an engagement folder (from project-mcp). If provided, writes to <engagement_path>/onewire/ instead of creating a standalone engagement.",
                },
            },
            "required": ["engagement_name"],
        },
    ),
    Tool(
        name="onewire_search",
        description=(
            "Search for a device on the 1-Wire bus using Read ROM (0x33). "
            "Returns family code, serial number, and CRC validity. "
            "Works when exactly one device is present. "
            "[read-only] [Duration: fast (~1-3 s -- Read ROM sequence).]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
            },
            "required": ["session_id"],
        },
    ),
    Tool(
        name="onewire_read",
        description=(
            "Send arbitrary bytes on the 1-Wire bus and read back a response. "
            "[read-only] [Duration: instant.]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "write_hex": {"type": "string", "description": "Hex string of bytes to send (e.g. 'CC44')"},
                "read_bytes": {"type": "integer", "description": "Number of bytes to read back"},
            },
            "required": ["session_id", "write_hex", "read_bytes"],
        },
    ),
    Tool(
        name="close_1wire",
        description="Close a 1-Wire session and reset the ESP32-Bit-Pirate to HiZ mode. [allowed-write] [Duration: instant.]",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
            },
            "required": ["session_id"],
        },
    ),
    Tool(
        name="measure_pin",
        description=(
            "Full signal analysis of a GPIO pin: edge rate, duty cycle, "
            "approximate frequency, jitter, and a classified signal guess "
            "(Idle/Noise/Clock/PWM/Servo/DataLike/Burst) from the firmware "
            "PinAnalyzer. [read-only] [Duration: fast (samples for duration_ms, default 2000).]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "pin": {"type": "integer", "description": "ESP32 GPIO to analyze (0-48)"},
                "duration_ms": {
                    "type": "integer",
                    "default": 2000,
                    "description": "Sample window (ms, max 8000)",
                },
            },
            "required": ["pin"],
        },
    ),
    Tool(
        name="measure_frequency",
        description=(
            "Measure the SCL clock frequency of an I2C bus (or any clock on a "
            "pin) using the hardware RMT analyzer. Returns frequency, period, "
            "LOW/HIGH times, and cycle counts. [read-only] [Duration: fast (measures for duration_ms, default 2000).]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "duration_ms": {
                    "type": "integer",
                    "default": 2000,
                    "description": "Measurement window (ms, max 10000)",
                },
                "scl_pin": {
                    "type": "integer",
                    "description": "Optional GPIO to measure as SCL (reconfigures I2C SCL, then restores)",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="capture",
        description=(
            "Capture a logic waveform on a GPIO pin as a compact hex stream "
            "(one hex char per 4 samples) plus the expanded level list. "
            "[read-only] [Duration: fast (samples * delay_us).]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "pin": {"type": "integer", "description": "ESP32 GPIO to capture (0-48)"},
                "samples": {
                    "type": "integer",
                    "default": 256,
                    "description": "Number of samples (max 1024)",
                },
                "delay_us": {
                    "type": "integer",
                    "default": 100,
                    "description": "Sample interval (us, max 10000)",
                },
            },
            "required": ["pin"],
        },
    ),
    Tool(
        name="set_pin",
        description=(
            "Drive a GPIO pin HIGH (level=true) or LOW (level=false). "
            "[write] [Duration: fast.]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "pin": {"type": "integer", "description": "ESP32 GPIO (0-48)"},
                "level": {"type": "boolean", "description": "true=HIGH, false=LOW"},
            },
            "required": ["pin", "level"],
        },
    ),
    Tool(
        name="read_pin",
        description=(
            "Read a GPIO pin and return its level (0=LOW, 1=HIGH). "
            "[read-only] [Duration: fast.]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "pin": {"type": "integer", "description": "ESP32 GPIO (0-48)"},
            },
            "required": ["pin"],
        },
    ),
    Tool(
        name="pulse",
        description=(
            "Generate an inverse pulse on a GPIO pin for duration_us microseconds "
            "(drives the opposite of the current level, then restores). "
            "[write] [Duration: fast (duration_us).]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "pin": {"type": "integer", "description": "ESP32 GPIO (0-48)"},
                "duration_us": {"type": "integer", "description": "Pulse width (us)"},
            },
            "required": ["pin", "duration_us"],
        },
    ),
    Tool(
        name="pull_up",
        description=(
            "Enable the internal pull-up resistor on a GPIO pin. "
            "[write] [Duration: fast.]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "pin": {"type": "integer", "description": "ESP32 GPIO (0-48)"},
            },
            "required": ["pin"],
        },
    ),
    Tool(
        name="pull_down",
        description=(
            "Enable the internal pull-down resistor on a GPIO pin. "
            "[write] [Duration: fast.]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "pin": {"type": "integer", "description": "ESP32 GPIO (0-48)"},
            },
            "required": ["pin"],
        },
    ),
    Tool(
        name="pwm_set",
        description=(
            "Start PWM output on a GPIO pin at freq_hz with duty_pct percent duty. "
            "Use pwm_stop to stop it. [write] [Duration: fast.]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "pin": {"type": "integer", "description": "ESP32 GPIO (0-48)"},
                "freq_hz": {"type": "integer", "description": "PWM frequency (Hz)"},
                "duty_pct": {"type": "integer", "description": "Duty cycle (1-100 %)"},
            },
            "required": ["pin", "freq_hz", "duty_pct"],
        },
    ),
    Tool(
        name="pwm_stop",
        description=(
            "Stop PWM on a GPIO pin and reset it to a plain input (no pull). "
            "[write] [Duration: fast.]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "pin": {"type": "integer", "description": "ESP32 GPIO (0-48)"},
            },
            "required": ["pin"],
        },
    ),
]


@app.list_tools()
async def list_tools():
    return TOOL_DEFINITIONS


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name in {"open_uart", "open_spi", "open_i2c", "open_1wire"} and "project_path" in arguments:
        return [TextContent(type="text", text=json.dumps(
            {"error": "project_path was renamed to engagement_path in v0.3; "
                      "pass engagement_path instead.", "tool": name}, indent=2))]
    tier = classify_tool(name)
    logger.info("tool=%s tier=%s args=%s", name, tier.value, arguments)

    # Approval-write tools require explicit confirmation token.
    # MCP clients that respect destructiveHint will prompt the user,
    # but we enforce server-side too for clients that don't.
    if tier == SafetyTier.APPROVAL_WRITE:
        # Strict: _confirmed must be the literal boolean True. Truthy values
        # like 1, "true", or "yes" do NOT satisfy the gate -- they often leak
        # in from loose JSON decoders or mistakes in client code.
        if arguments.get("_confirmed") is not True:
            desc = f"{name}({', '.join(f'{k}={v}' for k, v in arguments.items())})"
            return [TextContent(
                type="text",
                text=json.dumps({
                    "confirmation_required": True,
                    "tool": name,
                    "arguments": arguments,
                    "message": f"APPROVAL REQUIRED: {desc}. "
                    f"Re-call with _confirmed=true (boolean) to execute.",
                }),
            )]
        # Remove the confirmation token before passing to the tool
        arguments = {k: v for k, v in arguments.items() if k != "_confirmed"}

    try:
        if name == "list_devices":
            result = await tools.tool_list_devices()

        elif name == "verify_connection":
            hw = _get_hardware()
            result = await tools.tool_verify_connection(
                hardware=hw,
                pins={"tx": arguments["tx_pin"], "rx": arguments["rx_pin"]},
                sample_duration_ms=arguments.get("sample_duration_ms", 2000),
            )

        elif name == "scan_baud":
            hw = _get_hardware()
            result = await tools.tool_scan_baud(hardware=hw)

        elif name == "open_uart":
            hw = _get_hardware()
            result = await tools.tool_open_uart(
                session_manager=session_manager,
                hardware=hw,
                baud=arguments["baud"],
                pins={
                    "tx": arguments.get("tx_pin", 4),
                    "rx": arguments.get("rx_pin", 5),
                },
                engagement_name=arguments["engagement_name"],
                device_path=_hardware_port,
                engagement_path=arguments.get("engagement_path"),
            )

        elif name == "read_output":
            result = await tools.tool_read_output(
                session_manager=session_manager,
                session_id=arguments["session_id"],
                timeout_ms=arguments.get("timeout_ms", 5000),
            )

        elif name == "send_command":
            result = await tools.tool_send_command(
                session_manager=session_manager,
                session_id=arguments["session_id"],
                command=arguments["command"],
                timeout_ms=arguments.get("timeout_ms", 3000),
            )

        elif name == "close_uart":
            result = await tools.tool_close_uart(
                session_manager=session_manager,
                session_id=arguments["session_id"],
            )

        elif name == "set_voltage":
            hw = _get_hardware()
            result = await tools.tool_set_voltage(
                hardware=hw,
                voltage_v=arguments["voltage_v"],
                current_limit_ma=arguments["current_limit_ma"],
            )

        elif name == "set_power":
            hw = _get_hardware()
            result = await tools.tool_set_power(
                hardware=hw,
                enable=arguments["enable"],
            )

        elif name == "enter_download_mode":
            hw = _get_hardware()
            result = await tools.tool_enter_download_mode(
                hardware=hw,
                boot_pin=arguments["boot_pin"],
                reset_pin=arguments["reset_pin"],
            )

        elif name == "read_flash":
            hw = _get_hardware()
            terminal_port = BitPirateHardware.find_terminal_port()
            if not terminal_port:
                result = {"error": "Cannot find Bit-Pirate console port for bridge mode"}
            else:
                result = await tools.tool_read_flash(
                    hardware=hw,
                    terminal_port=terminal_port,
                    offset=arguments.get("offset", 0),
                    size=arguments["size"],
                    output_path=arguments["output_path"],
                    boot_pin=arguments.get("boot_pin", -1),
                    reset_pin=arguments.get("reset_pin", -1),
                    baud=arguments.get("baud", 115200),
                )

        # --- SPI ---
        elif name == "open_spi":
            hw = _get_hardware()
            result = await tools.tool_open_spi(
                session_manager=session_manager,
                hardware=hw,
                engagement_name=arguments["engagement_name"],
                engagement_path=arguments.get("engagement_path"),
            )

        elif name == "spi_probe":
            result = await tools.tool_spi_probe(hardware=_get_hardware())

        elif name == "spi_read":
            result = await tools.tool_spi_read(
                hardware=_get_hardware(),
                address=arguments["address"],
                length=arguments["length"],
                timeout=arguments.get("timeout", 120),
            )

        elif name == "spi_dump":
            result = await tools.tool_spi_dump(
                hardware=_get_hardware(),
                output_path=arguments["output_path"],
                timeout=arguments.get("timeout"),
            )

        elif name == "spi_write":
            result = await tools.tool_spi_write(
                hardware=_get_hardware(),
                address=arguments["address"],
                input_path=arguments.get("input_path"),
                data_hex=arguments.get("data_hex"),
                ascii_string=arguments.get("ascii_string"),
                auto_erase=arguments.get("auto_erase", True),
                timeout=arguments.get("timeout", 120),
            )

        elif name == "spi_transfer":
            result = await tools.tool_spi_transfer(
                hardware=_get_hardware(),
                instruction=arguments["instruction"],
                timeout=arguments.get("timeout", 15),
            )

        elif name == "spi_config":
            result = await tools.tool_spi_config(
                hardware=_get_hardware(),
                mosi=arguments["mosi"],
                miso=arguments["miso"],
                sclk=arguments["sclk"],
                cs=arguments["cs"],
                freq_mhz=arguments["freq_mhz"],
                wp=arguments.get("wp", -1),
                hold=arguments.get("hold", -1),
            )

        elif name == "spi_flash_analyze":
            result = await tools.tool_spi_flash_analyze(
                hardware=_get_hardware(),
                timeout=arguments.get("timeout", 600),
            )

        elif name == "spi_flash_search":
            result = await tools.tool_spi_flash_search(
                hardware=_get_hardware(),
                pattern=arguments["pattern"],
                timeout=arguments.get("timeout", 600),
            )

        elif name == "spi_flash_strings":
            result = await tools.tool_spi_flash_strings(
                hardware=_get_hardware(),
                min_length=arguments.get("min_length", 10),
                timeout=arguments.get("timeout", 600),
            )

        elif name == "spi_flash_erase":
            result = await tools.tool_spi_flash_erase(
                hardware=_get_hardware(),
                timeout=arguments.get("timeout", 1800),
            )

        elif name == "close_spi":
            result = await tools.tool_close_spi(
                session_manager=session_manager,
                session_id=arguments["session_id"],
                hardware=_get_hardware(),
            )

        # --- I2C ---
        elif name == "open_i2c":
            hw = _get_hardware()
            result = await tools.tool_open_i2c(
                session_manager=session_manager,
                hardware=hw,
                engagement_name=arguments["engagement_name"],
                speed=arguments.get("speed", 400_000),
                clock_stretch=arguments.get("clock_stretch", False),
                voltage_mv=arguments.get("voltage_mv"),
                current_ma=arguments.get("current_ma"),
                engagement_path=arguments.get("engagement_path"),
            )

        elif name == "i2c_scan":
            result = await tools.tool_i2c_scan(
                session_manager=session_manager,
                session_id=arguments["session_id"],
            )

        elif name == "i2c_read":
            result = await tools.tool_i2c_read(
                session_manager=session_manager,
                session_id=arguments["session_id"],
                device_addr=arguments["device_addr"],
                register_addr=arguments.get("register_addr"),
                length=arguments.get("length", 1),
            )

        elif name == "i2c_write":
            result = await tools.tool_i2c_write(
                session_manager=session_manager,
                session_id=arguments["session_id"],
                device_addr=arguments["device_addr"],
                register_addr=arguments["register_addr"],
                data_hex=arguments["data_hex"],
            )

        elif name == "i2c_dump":
            result = await tools.tool_i2c_dump(
                session_manager=session_manager,
                session_id=arguments["session_id"],
                device_addr=arguments["device_addr"],
                size=arguments.get("size", 256),
                register_bytes=arguments.get("register_bytes", 1),
            )

        elif name == "close_i2c":
            result = await tools.tool_close_i2c(
                session_manager=session_manager,
                session_id=arguments["session_id"],
                hardware=_get_hardware(),
            )

        # --- scope / measure (Phase 5) ---
        elif name == "measure_pin":
            hw = _get_hardware()
            result = await tools.tool_measure_pin(
                hardware=hw,
                pin=arguments["pin"],
                duration_ms=arguments.get("duration_ms", 2000),
            )

        elif name == "measure_frequency":
            hw = _get_hardware()
            result = await tools.tool_measure_frequency(
                hardware=hw,
                duration_ms=arguments.get("duration_ms", 2000),
                scl_pin=arguments.get("scl_pin"),
            )

        elif name == "capture":
            hw = _get_hardware()
            result = await tools.tool_capture(
                hardware=hw,
                pin=arguments["pin"],
                samples=arguments.get("samples", 256),
                delay_us=arguments.get("delay_us", 100),
            )

        # --- Tier 3: DIO digital + PWM ---
        elif name == "set_pin":
            hw = _get_hardware()
            result = await tools.tool_set_pin(
                hardware=hw,
                pin=arguments["pin"],
                level=arguments["level"],
            )

        elif name == "read_pin":
            hw = _get_hardware()
            result = await tools.tool_read_pin(
                hardware=hw,
                pin=arguments["pin"],
            )

        elif name == "pulse":
            hw = _get_hardware()
            result = await tools.tool_pulse(
                hardware=hw,
                pin=arguments["pin"],
                duration_us=arguments["duration_us"],
            )

        elif name == "pull_up":
            hw = _get_hardware()
            result = await tools.tool_pull_up(
                hardware=hw,
                pin=arguments["pin"],
            )

        elif name == "pull_down":
            hw = _get_hardware()
            result = await tools.tool_pull_down(
                hardware=hw,
                pin=arguments["pin"],
            )

        elif name == "pwm_set":
            hw = _get_hardware()
            result = await tools.tool_pwm_set(
                hardware=hw,
                pin=arguments["pin"],
                freq_hz=arguments["freq_hz"],
                duty_pct=arguments["duty_pct"],
            )

        elif name == "pwm_stop":
            hw = _get_hardware()
            result = await tools.tool_pwm_stop(
                hardware=hw,
                pin=arguments["pin"],
            )

        # --- 1-Wire ---
        elif name == "open_1wire":
            hw = _get_hardware()
            result = await tools.tool_open_1wire(
                session_manager=session_manager,
                hardware=hw,
                engagement_name=arguments["engagement_name"],
                voltage_mv=arguments.get("voltage_mv"),
                current_ma=arguments.get("current_ma"),
                engagement_path=arguments.get("engagement_path"),
            )

        elif name == "onewire_search":
            result = await tools.tool_onewire_search(
                session_manager=session_manager,
                session_id=arguments["session_id"],
            )

        elif name == "onewire_read":
            result = await tools.tool_onewire_read(
                session_manager=session_manager,
                session_id=arguments["session_id"],
                write_hex=arguments["write_hex"],
                read_bytes=arguments["read_bytes"],
            )

        elif name == "close_1wire":
            result = await tools.tool_close_1wire(
                session_manager=session_manager,
                session_id=arguments["session_id"],
                hardware=_get_hardware(),
            )

        else:
            result = {"error": f"Unknown tool: {name}"}

    except Exception as exc:
        logger.error("tool=%s error=%s", name, exc)
        result = {"error": str(exc), "tool": name}

    return [TextContent(type="text", text=json.dumps(result, indent=2))]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
