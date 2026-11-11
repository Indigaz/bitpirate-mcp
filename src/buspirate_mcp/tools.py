"""MCP tool implementations for BusPirate UART operations.

Each function is an async tool handler. Registration with the MCP
server happens in server.py. These functions contain the logic only.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from buspirate_mcp.bitpirate_hardware import BitPirateHardware, PSUUnavailableError
from buspirate_mcp.safety import validate_voltage_range
from buspirate_mcp.session import SessionManager

COMMON_BAUD_RATES = [115200, 57600, 38400, 19200, 9600, 4800, 2400, 1200]

ONEWIRE_FAMILIES = {
    0x01: "DS1990A (iButton)",
    0x10: "DS18S20 (Temperature)",
    0x22: "DS1822 (Temperature)",
    0x28: "DS18B20 (Temperature)",
    0x26: "DS2438 (Battery Monitor)",
    0x29: "DS2408 (8-ch Switch)",
    0x3A: "DS2413 (2-ch Switch)",
}
ONEWIRE_CMD_READ_ROM = 0x33

I2C_KNOWN_DEVICES = {
    range(0x50, 0x58): "EEPROM (24Cxx series)",
    range(0x68, 0x6A): "RTC/IMU (DS3231, MPU6050)",
    range(0x76, 0x78): "Pressure/Temp (BME280, BMP280)",
    range(0x3C, 0x3E): "OLED Display (SSD1306)",
    range(0x48, 0x50): "ADC (ADS1115, PCF8591)",
    range(0x20, 0x28): "IO Expander (PCF8574, MCP23017)",
    range(0x38, 0x40): "Touch/Humidity (FT6236, HTU21D)",
}

PROMPT_PATTERN = re.compile(r"^\s*[#$>\]]", re.MULTILINE)
BAUD_SCAN_READ_TIME_S = 0.5
SILENCE_THRESHOLD_S = 0.5


def _ascii_score(data: bytes) -> float:
    """Score data by ratio of printable ASCII to total bytes."""
    if not data:
        return 0.0
    printable = sum(
        1 for b in data if 32 <= b <= 126 or b in (9, 10, 13)
    )
    return printable / len(data)


async def tool_list_devices() -> dict[str, Any]:
    """List BusPirate devices on USB."""
    devices = BitPirateHardware.list_devices()
    result: dict[str, Any] = {"devices": devices}
    if not devices:
        result["hint"] = (
            "No BusPirate found. Check USB cable, run lsusb, "
            "verify permissions on /dev/ttyACM*."
        )
    return result


async def tool_verify_connection(
    hardware: Any,
    pins: dict[str, int],
    sample_duration_ms: int = 2000,
) -> dict[str, Any]:
    """Check for signal activity on the specified pins.

    Uses the firmware's DIO measure (edge counter + approximate frequency)
    as the activity detector -- this board has no per-pin ADC voltage read.
    """
    tx_pin = pins["tx"]
    rx_pin = pins["rx"]
    hardware._validate_pin(tx_pin)
    hardware._validate_pin(rx_pin)
    dur = min(int(sample_duration_ms), 5000)  # firmware caps measure at 5 s

    tx = hardware.measure_pin(tx_pin, dur)
    rx = hardware.measure_pin(rx_pin, dur)

    transitions = tx["rising"] + tx["falling"] + rx["rising"] + rx["falling"]
    freq_hz = max(tx["frequency_hz"], rx["frequency_hz"])
    activity = transitions > 0

    if activity:
        message = (
            f"Activity on TX: {tx['rising'] + tx['falling']} edges "
            f"({tx['frequency_hz']:.2f} Hz); RX: {rx['rising'] + rx['falling']} edges "
            f"({rx['frequency_hz']:.2f} Hz) over {dur} ms."
        )
    else:
        message = (
            f"No edges detected on TX ({tx_pin}) or RX ({rx_pin}) in {dur} ms. "
            "Check wiring, or the target may be idle."
        )

    return {
        "activity_detected": activity,
        "transitions": transitions,
        "approx_frequency_hz": round(freq_hz, 2),
        "pins": {
            "tx": {"pin": tx_pin, **tx},
            "rx": {"pin": rx_pin, **rx},
        },
        "sample_duration_ms": dur,
        "message": message,
    }


async def tool_scan_baud(
    hardware: Any,
) -> dict[str, Any]:
    """Scan common baud rates and score by readable ASCII output."""
    candidates = []

    for rate in COMMON_BAUD_RATES:
        hardware.configure_uart(speed=rate)
        hardware.read()  # flush stale bytes from previous rate
        await asyncio.sleep(BAUD_SCAN_READ_TIME_S)
        data = hardware.read() or b""
        score = _ascii_score(data)
        candidates.append({
            "baud": rate,
            "score": round(score, 3),
            "sample_bytes": len(data),
            "preview": data[:64].decode("utf-8", errors="replace"),
        })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    best = candidates[0] if candidates else {"score": 0.0}

    return {
        "candidates": candidates,
        "recommended": candidates[0]["baud"] if best["score"] >= 0.7 else None,
        "best_score": best["score"],
        "message": (
            f"Recommended baud rate: {candidates[0]['baud']} "
            f"(confidence: {best['score']:.1%})"
            if best["score"] >= 0.7
            else "No baud rate scored above 0.7 threshold. "
            "Provide all scores for user decision."
        ),
    }


async def tool_open_uart(
    session_manager: SessionManager,
    hardware: Any,
    baud: int,
    pins: dict[str, str],
    engagement_name: str,
    device_path: str = "",
    engagement_path: str | None = None,
) -> dict[str, Any]:
    """Open a persistent UART session and start logging."""
    hardware.configure_uart(speed=baud)
    session = session_manager.create(
        name=engagement_name,
        hardware=hardware,
        baud=baud,
        pins=pins,
        device_path=device_path,
        engagement_path=engagement_path,
    )
    return {
        "session_id": session.session_id,
        "engagement_path": str(session.engagement_path),
        "baud": baud,
        "pins": pins,
    }


async def tool_read_output(
    session_manager: SessionManager,
    session_id: str,
    timeout_ms: int = 5000,
) -> dict[str, Any]:
    """Read available data from an open UART session."""
    session = session_manager.get(session_id)
    collected = b""
    end_time = time.monotonic() + (timeout_ms / 1000.0)

    while time.monotonic() < end_time:
        chunk = session.hardware.read() or b""
        if chunk:
            collected += chunk
            session.log_rx(chunk)
            if PROMPT_PATTERN.search(
                collected.decode("utf-8", errors="replace")
            ):
                break
        await asyncio.sleep(0.05)  # always yield to event loop

    text = collected.decode("utf-8", errors="replace")
    return {
        "text": text,
        "bytes_received": len(collected),
    }


async def tool_send_command(
    session_manager: SessionManager,
    session_id: str,
    command: str,
    timeout_ms: int = 3000,
) -> dict[str, Any]:
    """Send a command and capture the response."""
    session = session_manager.get(session_id)
    cmd_bytes = (command + "\r\n").encode("utf-8")
    session.hardware.write(cmd_bytes)
    session.log_tx(cmd_bytes)

    collected = b""
    last_rx_time = time.monotonic()
    end_time = time.monotonic() + (timeout_ms / 1000.0)

    while time.monotonic() < end_time:
        chunk = session.hardware.read() or b""
        if chunk:
            collected += chunk
            session.log_rx(chunk)
            last_rx_time = time.monotonic()

            decoded = collected.decode("utf-8", errors="replace")
            if PROMPT_PATTERN.search(decoded):
                break
        elif collected and (time.monotonic() - last_rx_time) > SILENCE_THRESHOLD_S:
            break
        await asyncio.sleep(0.05)  # always yield to event loop

    response = collected.decode("utf-8", errors="replace")
    return {
        "response": response,
        "bytes_received": len(collected),
    }


async def tool_close_uart(
    session_manager: SessionManager,
    session_id: str,
) -> dict[str, Any]:
    """Close a UART session and finalize logs."""
    session_manager.close(session_id)
    return {"closed": True, "session_id": session_id}


async def tool_set_voltage(
    hardware: Any,
    voltage_v: float,
    current_limit_ma: int,
) -> dict[str, Any]:
    """Set power supply voltage. Validates range before applying."""
    try:
        validate_voltage_range(voltage_v, current_limit_ma)
    except ValueError as e:
        return {"applied": False, "error": str(e)}

    try:
        result = hardware.set_voltage(voltage_v, current_limit_ma)
    except PSUUnavailableError as e:
        return {"applied": False, "psu": "unavailable", "error": str(e)}
    return {
        "applied": result,
        "voltage_v": voltage_v,
        "current_limit_ma": current_limit_ma,
        "error": None,
    }


async def tool_set_power(
    hardware: Any,
    enable: bool,
) -> dict[str, Any]:
    """Enable or disable power supply."""
    try:
        result = hardware.set_power(enable)
    except PSUUnavailableError as e:
        return {"applied": False, "psu": "unavailable", "error": str(e)}
    return {
        "applied": result,
        "power": "on" if enable else "off",
    }


async def tool_enter_download_mode(
    hardware: Any,
    boot_pin: int,
    reset_pin: int,
) -> dict[str, Any]:
    """Put target into UART download mode via GPIO pin toggling.

    Holds boot_pin LOW, pulses reset_pin LOW then HIGH, then releases
    boot_pin. Works for ESP32, ESP8266, and other chips with UART
    bootloaders triggered by a strapping pin.
    """
    if not isinstance(boot_pin, int) or boot_pin < 0 or boot_pin > 7:
        return {"error": f"boot_pin must be 0-7, got: {boot_pin}"}
    if not isinstance(reset_pin, int) or reset_pin < 0 or reset_pin > 7:
        return {"error": f"reset_pin must be 0-7, got: {reset_pin}"}

    # Hold boot select pin LOW
    hardware.set_pin_output(boot_pin, high=False)
    await asyncio.sleep(0.3)

    # Pulse reset: LOW then HIGH
    hardware.set_pin_output(reset_pin, high=False)
    await asyncio.sleep(0.2)
    hardware.set_pin_output(reset_pin, high=True)
    await asyncio.sleep(0.5)

    # Release boot pin
    hardware.release_pin(boot_pin)
    hardware.release_pin(reset_pin)

    return {
        "status": "download_mode_entered",
        "boot_pin": boot_pin,
        "reset_pin": reset_pin,
        "message": (
            f"Boot pin IO{boot_pin} toggled, reset pin IO{reset_pin} pulsed. "
            f"Target should be in UART download mode."
        ),
    }


def _enter_bridge_mode(terminal_port: str) -> bool:
    """Enter UART bridge mode on the Bit-Pirate console port.

    The bridge command makes ACM0 a transparent serial passthrough
    to the target. Handles VT100 prompt, detects current mode, and
    enters bridge. Returns True on success.
    """
    import serial
    ser = serial.Serial(terminal_port, 115200, timeout=2)
    try:
        time.sleep(0.5)
        ser.read(ser.in_waiting or 1)

        # Send enter to see what prompt we get
        ser.write(b'\r')
        time.sleep(0.5)
        resp = ser.read(ser.in_waiting or 1).decode('utf-8', errors='replace')

        # Handle VT100 prompt (appears after fresh USB plug)
        if 'VT100' in resp or resp.strip() == '':
            ser.write(b'n\r')
            time.sleep(0.5)
            resp = ser.read(ser.in_waiting or 1).decode('utf-8', errors='replace')

        # Check if we're already in UART mode
        if 'UART>' in resp or 'UART>' in resp.upper():
            # Already in UART mode, send bridge
            ser.write(b'bridge\r')
            time.sleep(1)
            resp = ser.read(ser.in_waiting or 1).decode('utf-8', errors='replace')
            return 'bridge' in resp.lower()

        if 'HiZ>' in resp:
            # Need to enter UART mode first
            ser.write(b'm\r')
            time.sleep(0.5)
            ser.read(ser.in_waiting or 1)
            ser.write(b'3\r')
            time.sleep(1)
            ser.read(ser.in_waiting or 1)
            ser.write(b'y\r')
            time.sleep(0.5)
            ser.read(ser.in_waiting or 1)

        # Try bridge
        ser.write(b'bridge\r')
        time.sleep(1)
        resp = ser.read(ser.in_waiting or 1).decode('utf-8', errors='replace')
        return 'bridge' in resp.lower()
    except Exception:
        return False
    finally:
        ser.close()


def _exit_bridge_mode(terminal_port: str) -> None:
    """Attempt to exit UART bridge mode on the Bit-Pirate.

    Bridge mode exits when the Bit-Pirate button is pressed, or when the
    serial port is closed with a break condition. We send a serial
    break and toggle DTR/RTS to signal the Bit-Pirate to drop bridge mode.
    """
    import serial
    try:
        ser = serial.Serial(terminal_port, 115200, timeout=1)
        ser.send_break(duration=0.5)
        ser.dtr = False
        ser.rts = False
        time.sleep(0.3)
        ser.dtr = True
        ser.rts = True
        time.sleep(0.3)
        ser.close()
    except Exception:
        pass  # best effort


async def tool_read_flash(
    hardware: Any,
    terminal_port: str,
    offset: int,
    size: int,
    output_path: str,
    boot_pin: int = -1,
    reset_pin: int = -1,
    baud: int = 115200,
) -> dict[str, Any]:
    """Read flash memory from target via esptool through the Bit-Pirate UART bridge.

    Optionally enters download mode first if boot_pin and reset_pin are
    provided. Then enters bridge mode on the terminal port and runs
    esptool read-flash.
    """
    # Validate pin numbers when they are being used
    if boot_pin >= 0 and (not isinstance(boot_pin, int) or boot_pin > 48):
        return {"error": f"boot_pin must be 0-48, got: {boot_pin}"}
    if reset_pin >= 0 and (not isinstance(reset_pin, int) or reset_pin > 48):
        return {"error": f"reset_pin must be 0-48, got: {reset_pin}"}

    # Step 1: Enter download mode if pins provided
    if boot_pin >= 0 and reset_pin >= 0:
        dl_result = await tool_enter_download_mode(
            hardware, boot_pin, reset_pin,
        )
        if dl_result["status"] != "download_mode_entered":
            return {"error": "Failed to enter download mode", "detail": dl_result}

    # Step 2: Enter bridge mode.
    # If bridge is already active from a previous read_flash call,
    # _enter_bridge_mode will send 'bridge\r' which the target ignores.
    # esptool will re-sync either way.
    _enter_bridge_mode(terminal_port)

    # Step 3: Run esptool
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    # Find esptool in the same venv as the MCP server
    esptool_bin = shutil.which('esptool')
    if not esptool_bin:
        # Fall back to looking next to the Python interpreter
        esptool_bin = str(Path(sys.executable).parent / 'esptool')
    cmd = [
        esptool_bin, '--port', terminal_port,
        '--before', 'no-reset', '--after', 'no-reset',
        '--no-stub', '--baud', str(baud),
        'read-flash', str(offset), str(size), str(output),
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=max(60, size // 1000),  # ~1s per KB at 115200 + margin
        )
    except FileNotFoundError:
        return {
            "error": "esptool not found. Install with: pip install esptool",
        }
    except subprocess.TimeoutExpired:
        return {
            "error": "esptool timed out",
            "command": ' '.join(cmd),
        }

    if result.returncode != 0:
        return {
            "error": "esptool failed",
            "returncode": result.returncode,
            "stderr": result.stderr[-500:],
            "command": ' '.join(cmd),
        }

    file_size = output.stat().st_size if output.exists() else 0

    # Step 4: Attempt cleanup.
    # Bridge mode on ACM0 cannot be exited programmatically (only via
    # the Bit-Pirate physical button). Protocol commands hang while bridge is
    # active. The user must USB-replug the Bit-Pirate after flash operations
    # to restore normal operation.
    # TODO: find a way to exit bridge mode without physical button

    return {
        "status": "success",
        "output_path": str(output),
        "bytes_read": file_size,
        "offset": offset,
        "size_requested": size,
        "baud": baud,
        "stdout": result.stdout[-300:],
        "cleanup": (
            "Bridge mode is still active. Press the Bit-Pirate button or "
            "USB-replug to exit bridge mode and restore normal operation."
        ),
    }


def _onewire_crc8(data: bytes) -> int:
    """CRC-8 Dallas/Maxim 1-Wire (polynomial 0x31, reflected as 0x8C)."""
    crc = 0
    for byte in data:
        for _ in range(8):
            mix = (crc ^ byte) & 0x01
            crc >>= 1
            if mix:
                crc ^= 0x8C
            byte >>= 1
    return crc


async def tool_open_1wire(
    session_manager: SessionManager,
    hardware: Any,
    engagement_name: str,
    voltage_mv: int | None = None,
    current_ma: int | None = None,
    engagement_path: str | None = None,
) -> dict[str, Any]:
    """Configure 1-Wire mode and open a transaction session."""
    hardware.configure_1wire(voltage_mv, current_ma)
    session = session_manager.create(
        name=engagement_name,
        hardware=hardware,
        protocol="1wire",
        protocol_config={
            "voltage_mv": voltage_mv,
            "current_ma": current_ma,
        },
        engagement_path=engagement_path,
    )
    return {
        "session_id": session.session_id,
        "engagement_path": str(session.engagement_path),
    }


async def tool_onewire_search(
    session_manager: SessionManager,
    session_id: str,
) -> dict[str, Any]:
    """Search for a device on the 1-Wire bus using Read ROM (0x33).

    Works when exactly one device is present. Returns family code, serial,
    CRC validity, and full ROM code. Returns {"present": False} if no device
    asserts a presence pulse.
    """
    session = session_manager.get(session_id)

    present = session.hardware.onewire_reset()
    if not present:
        return {"present": False}

    rom_bytes = session.hardware.onewire_transfer([ONEWIRE_CMD_READ_ROM], 8)
    if not rom_bytes or len(rom_bytes) < 8:
        return {"present": False}

    rom = bytes(rom_bytes)
    family_code = rom[0]
    serial_bytes = rom[1:7]
    crc_byte = rom[7]

    # CRC covers the first 7 bytes; the 8th byte is the CRC itself.
    crc_calc = _onewire_crc8(rom[:7])
    crc_valid = crc_calc == crc_byte

    family_name = ONEWIRE_FAMILIES.get(family_code, f"Unknown (0x{family_code:02X})")
    serial_hex = serial_bytes.hex().upper()
    rom_code = rom.hex().upper()

    session.log_transaction(
        operation="READ_ROM",
        write_hex=f"{ONEWIRE_CMD_READ_ROM:02X}",
        read_hex=rom.hex().upper(),
        metadata={
            "family_code": f"0x{family_code:02X}",
            "family_name": family_name,
            "serial": serial_hex,
            "crc_valid": crc_valid,
        },
    )

    return {
        "present": True,
        "family_code": f"0x{family_code:02X}",
        "family_name": family_name,
        "serial": serial_hex,
        "crc_valid": crc_valid,
        "rom_code": rom_code,
    }


async def tool_onewire_read(
    session_manager: SessionManager,
    session_id: str,
    write_hex: str,
    read_bytes: int,
) -> dict[str, Any]:
    """Send arbitrary bytes on the 1-Wire bus and read back a response.

    write_hex: hex string of bytes to write (e.g. "CC44").
    read_bytes: number of bytes to read after the write.
    """
    session = session_manager.get(session_id)

    data = bytes.fromhex(write_hex)
    result = session.hardware.onewire_transfer(list(data), read_bytes)
    result_bytes = bytes(result) if result else b""
    result_hex = result_bytes.hex().upper()

    session.log_transaction(
        operation="TRANSFER",
        write_hex=write_hex.upper(),
        read_hex=result_hex,
    )

    return {
        "tx": write_hex.upper(),
        "rx": result_hex,
        "bytes_read": len(result_bytes),
    }


async def tool_close_1wire(
    session_manager: SessionManager,
    session_id: str,
    hardware: Any,
) -> dict[str, Any]:
    """Close a 1-Wire session and reset the BusPirate to HiZ mode."""
    session_manager.close(session_id)
    hardware.reset_mode()
    return {"closed": True}


# ---------------------------------------------------------------------------
# I2C tools
# ---------------------------------------------------------------------------

def _i2c_hint(addr_7bit: int) -> str | None:
    """Look up a hint for a 7-bit I2C address from I2C_KNOWN_DEVICES."""
    for addr_range, hint in I2C_KNOWN_DEVICES.items():
        if addr_7bit in addr_range:
            return hint
    return None


def _parse_device_addr(device_addr: str | int) -> int:
    """Parse device_addr as hex string or int, return 7-bit int."""
    if isinstance(device_addr, str):
        return int(device_addr, 16)
    return device_addr


async def tool_open_i2c(
    session_manager: SessionManager,
    hardware: Any,
    engagement_name: str,
    speed: int = 400000,
    clock_stretch: bool = False,
    voltage_mv: int | None = None,
    current_ma: int | None = None,
    engagement_path: str | None = None,
) -> dict[str, Any]:
    """Open a persistent I2C session and start logging."""
    hardware.configure_i2c(speed, clock_stretch, voltage_mv, current_ma)
    session = session_manager.create(
        name=engagement_name,
        hardware=hardware,
        protocol="i2c",
        protocol_config={
            "speed": speed,
            "clock_stretch": clock_stretch,
            "voltage_mv": voltage_mv,
            "current_ma": current_ma,
        },
        engagement_path=engagement_path,
    )
    return {
        "session_id": session.session_id,
        "engagement_path": str(session.engagement_path),
        "i2c_config": {
            "speed": speed,
            "clock_stretch": clock_stretch,
            "voltage_mv": voltage_mv,
            "current_ma": current_ma,
        },
    }


async def tool_i2c_scan(
    session_manager: SessionManager,
    session_id: str,
) -> dict[str, Any]:
    """Scan the I2C bus for devices and return addresses with hints."""
    session = session_manager.get(session_id)
    raw_addresses = session.hardware.i2c_scan(0x00, 0x7F)

    # SDK returns raw (shifted) addresses -- convert to 7-bit and deduplicate
    seen: set[int] = set()
    devices = []
    for raw_addr in raw_addresses:
        addr_7bit = raw_addr >> 1
        if addr_7bit in seen:
            continue
        seen.add(addr_7bit)
        hint = _i2c_hint(addr_7bit)
        devices.append({
            "address": f"0x{addr_7bit:02X}",
            "address_7bit": addr_7bit,
            "hint": hint,
        })

    devices.sort(key=lambda d: d["address_7bit"])

    session.log_transaction(
        operation="scan",
        metadata={"found": len(devices), "addresses": [d["address"] for d in devices]},
    )

    return {"devices": devices, "count": len(devices)}


async def tool_i2c_read(
    session_manager: SessionManager,
    session_id: str,
    device_addr: str | int,
    register_addr: int | None = None,
    length: int = 1,
) -> dict[str, Any]:
    """Read bytes from an I2C device, optionally from a specific register."""
    session = session_manager.get(session_id)
    addr_7bit = _parse_device_addr(device_addr)
    addr_write = (addr_7bit << 1) & 0xFE

    if register_addr is not None:
        write_data = [addr_write, register_addr]
    else:
        write_data = [addr_write]

    read_bytes_raw = session.hardware.i2c_transfer(write_data, read_bytes=length)

    data_hex = read_bytes_raw.hex() if read_bytes_raw else ""
    data_list = list(read_bytes_raw) if read_bytes_raw else []

    write_hex = bytes(write_data).hex()
    session.log_transaction(
        operation="read",
        write_hex=write_hex,
        read_hex=data_hex,
        metadata={
            "address": f"0x{addr_7bit:02X}",
            "register": f"0x{register_addr:02X}" if register_addr is not None else None,
            "length": length,
        },
    )

    return {
        "address": f"0x{addr_7bit:02X}",
        "register": f"0x{register_addr:02X}" if register_addr is not None else None,
        "data_hex": data_hex,
        "data_bytes": data_list,
        "length": len(data_list),
    }


async def tool_i2c_write(
    session_manager: SessionManager,
    session_id: str,
    device_addr: str | int,
    register_addr: int,
    data_hex: str,
) -> dict[str, Any]:
    """Write bytes to an I2C device register."""
    session = session_manager.get(session_id)
    addr_7bit = _parse_device_addr(device_addr)
    addr_write = (addr_7bit << 1) & 0xFE

    data_bytes = list(bytes.fromhex(data_hex))
    write_data = [addr_write, register_addr] + data_bytes

    session.hardware.i2c_transfer(write_data, read_bytes=0)

    write_hex = bytes(write_data).hex()
    session.log_transaction(
        operation="write",
        write_hex=write_hex,
        metadata={
            "address": f"0x{addr_7bit:02X}",
            "register": f"0x{register_addr:02X}",
            "bytes_written": len(data_bytes),
        },
    )

    return {
        "written": True,
        "address": f"0x{addr_7bit:02X}",
        "register": f"0x{register_addr:02X}",
        "bytes_written": len(data_bytes),
    }


async def tool_i2c_dump(
    session_manager: SessionManager,
    session_id: str,
    device_addr: str | int,
    size: int = 256,
    register_bytes: int = 1,
) -> dict[str, Any]:
    """Dump memory from an I2C device by reading all registers sequentially.

    register_bytes=1 for devices with 8-bit register/memory addresses (most small
    EEPROMs, sensors). register_bytes=2 for 16-bit addresses (24C32+, larger
    EEPROMs) -- address sent MSB first.
    """
    if register_bytes not in (1, 2):
        return {"error": f"register_bytes must be 1 or 2, got {register_bytes}"}
    max_addr = 256 if register_bytes == 1 else 65536
    if size > max_addr:
        return {
            "error": f"size {size} exceeds {max_addr}-byte address space "
                     f"for register_bytes={register_bytes}"
        }

    session = session_manager.get(session_id)
    addr_7bit = _parse_device_addr(device_addr)
    addr_write = (addr_7bit << 1) & 0xFE

    chunk_size = 32
    all_data = bytearray()

    for offset in range(0, size, chunk_size):
        count = min(chunk_size, size - offset)
        if register_bytes == 1:
            write_data = [addr_write, offset & 0xFF]
        else:
            write_data = [addr_write, (offset >> 8) & 0xFF, offset & 0xFF]
        chunk = session.hardware.i2c_transfer(write_data, read_bytes=count)
        if chunk:
            all_data.extend(chunk)
        else:
            all_data.extend(b"\xff" * count)

    artifacts_dir = session.engagement_path / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    dump_file = artifacts_dir / f"i2c_dump_{addr_7bit:02x}.bin"
    dump_file.write_bytes(bytes(all_data))

    hex_preview = all_data[:64].hex()

    session.log_transaction(
        operation="dump",
        metadata={
            "address": f"0x{addr_7bit:02X}",
            "size": size,
            "register_bytes": register_bytes,
            "bytes_read": len(all_data),
            "file": str(dump_file),
        },
    )

    return {
        "bytes_read": len(all_data),
        "hex_preview": hex_preview,
        "file_path": str(dump_file),
    }


async def tool_close_i2c(
    session_manager: SessionManager,
    session_id: str,
    hardware: Any,
) -> dict[str, Any]:
    """Close an I2C session and reset the bus mode."""
    session_manager.close(session_id)
    hardware.reset_mode()
    return {"closed": True}


# ---------------------------------------------------------------------------
# SPI tools (ESP32-Bit-Pirate: native flash shell + instruction syntax)
# ---------------------------------------------------------------------------


def _human_size(n: int) -> str:
    """Format byte count as human-readable string."""
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.0f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


async def tool_open_spi(
    session_manager: SessionManager,
    hardware: Any,
    engagement_name: str,
    engagement_path: str | None = None,
) -> dict[str, Any]:
    """Enter SPI mode on the ESP32-Bit-Pirate and create an engagement session."""
    if getattr(hardware, "_active_mode", None) != "spi":
        hardware._set_mode("spi")
    session = session_manager.create(
        name=engagement_name,
        hardware=hardware,
        protocol="spi",
        protocol_config={},
        engagement_path=engagement_path,
    )
    return {
        "status": "open",
        "session_id": session.session_id,
        "protocol": "spi",
        "hint": (
            "Pin map comes from NVS (run spi_config to change it). "
            "Use spi_probe to detect the flash, spi_instruction for raw "
            "[0x..] transactions, or the spi_flash_* tools for the shell."
        ),
    }


async def tool_spi_probe(hardware: Any) -> dict[str, Any]:
    """Detect the SPI flash chip via the firmware flash shell (menu 1)."""
    r = hardware.spi_flash_probe()
    result = dict(r)
    if r.get("capacity_bytes"):
        result["capacity_human"] = _human_size(r["capacity_bytes"])
    return result


async def tool_spi_read(
    hardware: Any,
    address: int,
    length: int,
    timeout: int = 120,
) -> dict[str, Any]:
    """Read a region of SPI flash (hexdump, menu 5)."""
    transcript = hardware.spi_flash_read(address, length, timeout=timeout)
    return {
        "address": address,
        "length": length,
        "hexdump": transcript,
    }


async def tool_spi_dump(
    hardware: Any,
    output_path: str,
    timeout: int | None = None,
) -> dict[str, Any]:
    """Dump the entire SPI flash to a file (raw mode, menu 8)."""
    probe = hardware.spi_flash_probe()
    capacity = probe.get("capacity_bytes") or 0
    if not probe.get("present"):
        return {
            "error": "No SPI flash detected (bus error or missing chip). "
            "Check wiring and config first.",
        }
    if timeout is None:
        timeout = int(hardware._dump_timeout(capacity))
    r = hardware.spi_flash_dump_raw(output_path, capacity_bytes=capacity,
                                    timeout=float(timeout))
    return {
        "path": r["path"],
        "size": r["size"],
        "size_human": _human_size(r["size"]),
        "capacity_bytes": capacity,
    }


async def tool_spi_write(
    hardware: Any,
    address: int,
    input_path: str | None = None,
    data_hex: str | None = None,
    ascii_string: str | None = None,
    auto_erase: bool = True,
    timeout: int = 120,
) -> dict[str, Any]:
    """Write data to SPI flash (menu 6). Exactly one data source required."""
    sources = sum(x is not None for x in (input_path, data_hex, ascii_string))
    if sources != 1:
        return {
            "error": "Provide exactly one of input_path, data_hex, or "
            "ascii_string",
        }
    if input_path is not None:
        data = Path(input_path).read_bytes()
        data_hex = data.hex().upper()
        ascii_string = None
    result = hardware.spi_flash_write(
        address,
        data_hex=data_hex,
        ascii_string=ascii_string,
        auto_erase=auto_erase,
        timeout=float(timeout),
    )
    return {"status": "written", "address": address, "transcript": result}


async def tool_spi_transfer(
    hardware: Any,
    instruction: str,
    timeout: int = 15,
) -> dict[str, Any]:
    """Run a BP6-style SPI instruction, e.g. '[0x9F r:3]' (JEDEC read)."""
    rx = hardware.spi_instruction(instruction, timeout=float(timeout))
    return {
        "instruction": instruction,
        "rx_hex": rx.hex().upper() if rx else "",
        "bytes_read": len(rx),
    }


async def tool_spi_config(
    hardware: Any,
    mosi: int,
    miso: int,
    sclk: int,
    cs: int,
    freq_mhz: int,
    wp: int = -1,
    hold: int = -1,
    timeout: int = 15,
) -> dict[str, Any]:
    """Set the SPI pin map (NVS-persisted by the firmware)."""
    hardware.spi_config(mosi, miso, sclk, cs, freq_mhz, wp=wp, hold=hold,
                        timeout=float(timeout))
    return {
        "configured": True,
        "mosi": mosi, "miso": miso, "sclk": sclk, "cs": cs,
        "freq_mhz": freq_mhz, "wp": wp, "hold": hold,
    }


async def tool_spi_flash_analyze(
    hardware: Any,
    timeout: int = 600,
) -> dict[str, Any]:
    """On-device BinaryAnalyzer: file signatures, secret patterns, summary."""
    transcript = hardware.spi_flash_analyze(timeout=float(timeout))
    return {"transcript": transcript}


async def tool_spi_flash_search(
    hardware: Any,
    pattern: str,
    timeout: int = 600,
) -> dict[str, Any]:
    """Search the flash for a string pattern (1-32 bytes)."""
    return hardware.spi_flash_search(pattern, timeout=float(timeout))


async def tool_spi_flash_strings(
    hardware: Any,
    min_length: int = 10,
    timeout: int = 600,
) -> dict[str, Any]:
    """Extract printable strings from the whole flash."""
    transcript = hardware.spi_flash_strings(min_length, timeout=float(timeout))
    return {"transcript": transcript}


async def tool_spi_flash_erase(
    hardware: Any,
    timeout: int = 1800,
) -> dict[str, Any]:
    """Erase the ENTIRE flash chip (destructive, menu 9)."""
    transcript = hardware.spi_flash_erase(timeout=float(timeout))
    return {"status": "erased", "transcript": transcript}


async def tool_close_spi(
    session_manager: SessionManager,
    session_id: str,
    hardware: Any,
) -> dict[str, Any]:
    """Close an SPI session: exit the flash shell and return to HIZ mode."""
    session_manager.close(session_id)
    try:
        hardware._flash_exit(timeout=5.0)
    except Exception:
        pass
    if getattr(hardware, "_active_mode", None) != "hiz":
        try:
            hardware._set_mode("hiz")
        except Exception:
            pass
    hardware.reset_mode()
    return {"closed": True, "session_id": session_id}


# ---------------------------------------------------------------------------
# (Logic analyzer tools removed -- no FALA on this board; use DIO measure)
# ---------------------------------------------------------------------------
