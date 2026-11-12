"""Hardware driver for the ESP32-Bit-Pirate (v1.8) text CLI.

Replaces the upstream BPIO2-SDK hardware layer (see
/home/christian/.hermes/plans/2026-11-10_183841-bitpirate-mcp-port.md,
section 4). Talks to the firmware's UART0 console (default /dev/ttyACM0
@115200) with prompt-aware reads.

Phase 1 scope: core CLI primitives, DIO/GPIO, signal measurement
(`measure` edge/frequency detector), device discovery, PSU-unavailable
stubs. Protocol sessions (SPI/I2C/1-Wire/UART) land in later phases and
raise NotImplementedError until then.

Command grammar grounded in the v1.8 source:
  DioController.cpp    read/set/measure/pulse/toggle handlers
  UtilityController.cpp mode change, logic analyzer
  UserInputManager.cpp  menu/choice parsing (1-indexed)
"""

from __future__ import annotations

import re
import time
from glob import glob

import serial

DEFAULT_PORT: str = "/dev/ttyACM0"
CONSOLE_BAUD: int = 115200
SETTLE: float = 0.25
MAX_LINE_CHARS: int = 250
WAKE_TIMEOUT: float = 6.0
WAKE_QUIET: float = 0.6
PROMPT_TAIL_CHARS: int = 120

# S3 strapping/reserved pins (matches firmware's protected-pin list;
# verified against 99-Pinout wiki + skill notes).
PROTECTED_PINS: frozenset[int] = frozenset(
    {3, 19, 20, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 45, 46}
)

# Mode prompt ends a transcript: "HIZ> ", "DIO> ", "SPI> ", "I2C> " (digits!)
# Must match at the very END of the tail window (no multiline): a mid-text
# "X>" line is not a prompt.
PROMPT_RE: re.Pattern = re.compile(r"[A-Z0-9]{2,10}>[ \t]*\r?\s*$")

# Firmware error markers (DioController / I2cController / UserInputManager)
ERROR_MARKERS: tuple[bytes, ...] = (
    b"Invalid",
    b"Error:",
    b"Usage:",
    b"Unknown command",
    b"Missing mode",
    b"not allowed",
    b"protected",
)

RE_DIO_READ: re.Pattern = re.compile(rb"GPIO (\d+) = ([01]) \((?:HIGH|LOW)\)")
RE_MEASURE_RISING: re.Pattern = re.compile(rb"Rising edges:\s+(\d+)")
RE_MEASURE_FALLING: re.Pattern = re.compile(rb"Falling edges:\s+(\d+)")
RE_MEASURE_FREQ: re.Pattern = re.compile(rb"Approx\. frequency: ([\d.]+) Hz")

# -- SPI flash (v1.8 source: SpiFlashShell.cpp, UserInputManager.cpp) --
SPI_MENU_MARK = b"=== SPI Flash Shell ==="
DUMP_DONE_MARK = b"SPI Flash Dump: Done."
NO_FLASH = b"No SPI flash detected"
RAW_BYTES_PER_SECOND = 11520.0  # 115200 baud / 8, raw dump bottleneck

RE_FLASH_ID = re.compile(rb"SPI Flash ID: ([0-9A-Fa-f]{2}) ([0-9A-Fa-f]{2}) ([0-9A-Fa-f]{2})")
RE_FLASH_MFR = re.compile(rb"Manufacturer: (.+)")
RE_FLASH_MODEL = re.compile(rb"Model: (.+)")
RE_FLASH_CAP = re.compile(rb"Capacity: (\d+) bytes")
RE_FLASH_MATCH = re.compile(rb"^(0x[0-9A-Fa-f]{1,6}): (.+)$", re.MULTILINE)
RE_SPI_READ = re.compile(rb"SPI Read:\s*\n([0-9A-Fa-f ]+)")

from pathlib import Path



class PSUUnavailableError(RuntimeError):
    """Raised for PSU calls -- this board has no onboard power supply."""


class BitPirateCLIError(RuntimeError):
    """A CLI command returned an error transcript."""

    def __init__(self, message: str, transcript: str = "") -> None:
        super().__init__(message)
        self.transcript = transcript


class BitPirateHardware:
    """Wraps a single ESP32-Bit-Pirate console connection.

    Public API mirrors the upstream BusPirateHardware so the tool/dispatch
    layers compile unchanged; Bit-Pirate-specific methods are marked +.
    """

    def __init__(self, port: str = DEFAULT_PORT, baud: int = CONSOLE_BAUD) -> None:
        self.port: str = port
        self.baud: int = baud
        self.serial: serial.Serial | None = None
        self._active_mode: str | None = None
        self._flash_shell_open: bool = False
        self._last_voltage_mv: int = 0
        self._last_current_ma: int = 0

    # -- lifecycle --

    @classmethod
    def connect(cls, port: str = DEFAULT_PORT) -> "BitPirateHardware":
        obj = cls(port)
        s = serial.Serial(port, CONSOLE_BAUD, timeout=0.2)
        s.reset_input_buffer()
        obj.serial = s
        obj._wake()
        return obj

    @staticmethod
    def list_devices() -> list[dict[str, str]]:
        paths = sorted(glob("/dev/ttyACM*"))
        return [{"path": p, "role": "console"} for p in paths]

    @staticmethod
    def find_terminal_port() -> str | None:
        """Console port (for Phase 4 esptool bridge)."""
        paths = sorted(glob("/dev/ttyACM*"))
        return paths[0] if paths else None

    def _require_serial(self) -> serial.Serial:
        if self.serial is None:
            raise ConnectionError("Not connected. Call connect() first.")
        return self.serial

    def _wake(self) -> bytes:
        """Un-park 'Press any key to start' if present; drain the banner.

        If the firmware is parked inside the SPI flash shell menu (e.g. a
        previously crashed session left the shell open), line commands are
        eaten by the menu prompt, so exit the shell. Note the wake byte
        itself may run the shell's default action (Probe) first."""
        s = self._require_serial()
        s.write(b" \r")
        t = self._read_quiescent(timeout=WAKE_TIMEOUT, quiet=WAKE_QUIET)
        if b"=== SPI Flash Shell ===" in t or b"Enter index (default" in t:
            self._send_line("10")
            t += self._wait_marker(b"Exiting SPI Flash Shell", 10.0)
            self._flash_shell_open = False
        return t

    def _read_quiescent(self, timeout: float, quiet: float) -> bytes:
        s = self._require_serial()
        buf = b""
        deadline = time.monotonic() + timeout
        last = time.monotonic()
        while time.monotonic() < deadline:
            chunk = s.read(4096)
            if chunk:
                buf += chunk
                last = time.monotonic()
            elif not buf:
                break  # nothing arrived at all -- already quiescent
            elif time.monotonic() - last >= quiet:
                break
        return buf

    def _send_line(self, text: str) -> None:
        if len(text) > MAX_LINE_CHARS:
            raise ValueError(
                f"Command line exceeds {MAX_LINE_CHARS} chars (quadratic echo "
                f"limit): {len(text)}"
            )
        self._require_serial().write(text.encode("ascii") + b"\r")

    def _read_until_prompt(self, timeout: float) -> bytes:
        s = self._require_serial()
        buf = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = s.read(4096)
            if chunk:
                buf += chunk
                tail = buf[-PROMPT_TAIL_CHARS:].decode("utf-8", "replace")
                if PROMPT_RE.search(tail):
                    time.sleep(SETTLE)
                    buf += s.read(4096)
                    break
        return buf

    def _cmd(self, text: str, timeout: float = 6.0) -> bytes:
        self._send_line(text)
        t = self._read_until_prompt(timeout)
        decoded = t.decode("utf-8", "replace")
        for marker in ERROR_MARKERS:
            if marker in t:
                raise BitPirateCLIError(
                    f"CLI error for '{text}': {marker.decode()}", decoded
                )
        return t

    def _set_mode(self, name: str) -> bytes:
        t = self._cmd("mode " + name, timeout=8.0)
        self._active_mode = name
        return t

    def reset_mode(self) -> None:
        self._active_mode = None
        self._flash_shell_open = False

    def _validate_pin(self, pin: int) -> None:
        if not isinstance(pin, int) or isinstance(pin, bool):
            raise ValueError(f"Pin must be an int, got: {pin!r}")
        if pin < 0 or pin > 48:
            raise ValueError(f"Pin must be 0-48, got: {pin}")
        if pin in PROTECTED_PINS:
            raise ValueError(f"Pin {pin} is strapping-protected on this board")

    # -- DIO / GPIO (Phase 1) --

    def _dio(self, subcmd: str, *args: str) -> bytes:
        if self._active_mode != "dio":
            self._set_mode("dio")
        return self._cmd(" ".join([subcmd, *args]))

    def dio_set(self, pin: int, state: str) -> bytes:
        """+  DIO set: state in {I, O, H, L} (input/output/high/low)."""
        self._validate_pin(pin)
        if state not in ("I", "O", "H", "L"):
            raise ValueError(f"state must be I/O/H/L, got: {state}")
        return self._dio("set", str(pin), state)

    def read_pin(self, pin: int) -> int:
        self._validate_pin(pin)
        t = self._dio("read", str(pin))
        m = RE_DIO_READ.search(t)
        if not m:
            raise BitPirateCLIError(
                f"Cannot parse read result for pin {pin}", t.decode("utf-8", "replace")
            )
        return int(m.group(2))

    def set_pin_output(self, pin: int, high: bool) -> None:
        self._validate_pin(pin)
        self._dio("set", str(pin), "H" if high else "L")

    def configure_pin_input(self, pin: int) -> None:
        self._validate_pin(pin)
        self._dio("set", str(pin), "I")

    def release_pin(self, pin: int) -> None:
        self._validate_pin(pin)
        self._dio("set", str(pin), "I")

    def pulse_pin(self, pin: int, duration_us: int) -> None:
        """+  DIO pulse: inverse pulse (drives opposite level) for duration_us."""
        self._validate_pin(pin)
        self._dio("pulse", str(pin), str(int(duration_us)))

    def toggle_pin(self, pin: int, duration_ms: int) -> None:
        """+  DIO toggle: repeat H/L for duration_ms."""
        self._validate_pin(pin)
        self._dio("toggle", str(pin), str(int(duration_ms)))

    def measure_pin(self, pin: int, duration_ms: int = 2000) -> dict:
        """+  DIO measure: edge counter + approximate frequency (signal
        activity detector -- the verify_connection backend)."""
        self._validate_pin(pin)
        dur = min(int(duration_ms), 5000)
        t = self._dio("measure", str(pin), str(dur))
        r = RE_MEASURE_RISING.search(t)
        f = RE_MEASURE_FALLING.search(t)
        q = RE_MEASURE_FREQ.search(t)
        if not (r and f and q):
            raise BitPirateCLIError(
                f"Cannot parse measure result for pin {pin}",
                t.decode("utf-8", "replace"),
            )
        return {
            "rising": int(r.group(1)),
            "falling": int(f.group(1)),
            "frequency_hz": float(q.group(1)),
        }

    def get_pin_voltages(self) -> list[int] | None:
        """No per-pin ADC voltage on this board (DIO measure is an edge
        detector). Return None per upstream contract."""
        return None


    # -- SPI / flash (Phase 2) --

    def _wait_marker(self, marker: bytes, timeout: float) -> bytes:
        """Read console until `marker` appears; return all bytes read.

        Shell flows do not end at a mode prompt, so marker-based reads are
        used instead of _read_until_prompt.
        """
        s = self._require_serial()
        buf = b""
        window = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = s.read(4096)
            if not chunk:
                continue
            buf += chunk
            window += chunk
            if marker in window:
                return buf
            if len(window) > len(marker) - 1:
                window = window[-(len(marker) - 1):]
        raise BitPirateCLIError(
            f"Timed out after {timeout:.0f}s waiting for {marker[:40]!r}",
            buf.decode("utf-8", "replace")[-4000:],
        )

    def _spi(self, subcmd: str, *args: str) -> bytes:
        if self._flash_shell_open:
            # Instruction lines dispatch in SPI mode, NOT inside the shell's
            # menu loop (the menu would eat the line: "Invalid number").
            self._flash_exit()
        if self._active_mode != "spi":
            self._set_mode("spi")
        return self._cmd(" ".join([subcmd, *args]))

    def _flash_enter(self, timeout: float = 10.0) -> None:
        if self._flash_shell_open:
            return
        if self._active_mode != "spi":
            self._set_mode("spi")
        self._send_line("flash")
        self._wait_marker(SPI_MENU_MARK, timeout)
        self._flash_shell_open = True

    def _flash_exit(self, timeout: float = 10.0) -> bytes:
        if not self._flash_shell_open:
            return b""
        self._send_line("10")
        t = self._wait_marker(b"Exiting SPI Flash Shell", timeout)
        self._flash_shell_open = False
        return t

    def _flash_menu(self, idx: int, timeout: float) -> bytes:
        """Pick a shell action; read until the shell reprints its menu."""
        self._send_line(str(idx))
        return self._wait_marker(SPI_MENU_MARK, timeout)

    def spi_config(
        self,
        mosi: int,
        miso: int,
        sclk: int,
        cs: int,
        freq_mhz: int,
        wp: int = -1,
        hold: int = -1,
        timeout: float = 15.0,
    ) -> bytes:
        """Drive the 7-prompt config sequence (NVS-persisted by firmware)."""
        for p in (mosi, miso, sclk, cs):
            self._validate_pin(p)
        for p in (wp, hold):
            if p >= 0:
                self._validate_pin(p)
        if not 1 <= int(freq_mhz) <= 80:
            raise ValueError(f"freq_mhz must be 1-80, got: {freq_mhz}")
        if self._active_mode != "spi":
            self._set_mode("spi")
        self._send_line("config")
        for label, val in (("MOSI", mosi), ("MISO", miso), ("SCLK", sclk), ("CS", cs)):
            self._wait_marker((label + " GPIO [").encode(), timeout)
            self._send_line(str(int(val)))
        self._wait_marker(b"Frequency (MHz) [", timeout)
        self._send_line(str(int(freq_mhz)))
        self._wait_marker(b"Use WP or HOLD ? [", timeout)
        if wp >= 0 or hold >= 0:
            self._send_line("y")
            self._wait_marker(b"WP GPIO [", timeout)
            self._send_line(str(wp))
            self._wait_marker(b"HOLD GPIO [", timeout)
            self._send_line(str(hold))
        else:
            self._send_line("n")
        return self._wait_marker(b"SPI configured.", timeout)

    def spi_flash_probe(self, timeout: float = 15.0) -> dict:
        self._flash_enter()
        t = self._flash_menu(1, timeout)
        out: dict = {
            "present": False, "id": None, "manufacturer": None,
            "model": None, "capacity_bytes": None,
        }
        m = RE_FLASH_ID.search(t)
        if m:
            out["id"] = " ".join(g.decode().upper() for g in m.groups())
            out["present"] = m.group(1) not in (b"00", b"FF")
        if NO_FLASH in t:
            out["present"] = False
        mm = RE_FLASH_MFR.search(t)
        if mm:
            out["manufacturer"] = mm.group(1).decode().strip()
        mm = RE_FLASH_MODEL.search(t)
        if mm:
            out["model"] = mm.group(1).decode().strip()
        c = RE_FLASH_CAP.search(t)
        if c:
            out["capacity_bytes"] = int(c.group(1))
        return out

    def spi_flash_analyze(self, timeout: float = 600.0) -> str:
        """On-device BinaryAnalyzer: secrets, file signatures, summary."""
        self._flash_enter()
        t = self._flash_menu(2, timeout)
        return t.decode("utf-8", "replace")

    def spi_flash_search(self, pattern: str, timeout: float = 600.0) -> dict:
        if not 1 <= len(pattern) <= 32:
            raise ValueError(f"pattern must be 1-32 bytes, got {len(pattern)}")
        self._flash_enter()
        self._send_line("3")
        self._wait_marker(b"Enter string search pattern: ", timeout)
        self._send_line(pattern)
        t = self._wait_marker(b"Search complete.", timeout)
        matches = [
            {"address": a.decode(), "context": c.decode()}
            for a, c in RE_FLASH_MATCH.findall(t)
        ]
        return {
            "pattern": pattern,
            "matches": matches,
            "transcript": t.decode("utf-8", "replace"),
        }

    def spi_flash_strings(self, min_length: int = 10, timeout: float = 600.0) -> str:
        if not 2 <= int(min_length) <= 255:
            raise ValueError(f"min_length must be 2-255, got: {min_length}")
        self._flash_enter()
        self._send_line("4")
        self._wait_marker(b"Min. length of the strings: [", timeout)
        self._send_line(str(int(min_length)))
        t = self._wait_marker(b"SPI Flash: String extraction complete.", timeout)
        return t.decode("utf-8", "replace")

    def spi_flash_read(self, address: int, length: int, timeout: float = 120.0) -> str:
        if not (0 <= address < 0x10000000):
            raise ValueError(f"address must be 0-0xFFFFFF, got: {address:#x}")
        if not (0 < length <= 0x10000):
            raise ValueError(f"length must be 1-0x10000, got: {length}")
        self._flash_enter()
        self._send_line("5")
        self._wait_marker(b"Start address (dec or 0x hex) [", timeout)
        self._send_line(f"0x{address:X}")
        self._wait_marker(b"Number of bytes to read (dec or 0x hex) [", timeout)
        self._send_line(f"0x{length:X}")
        t = self._wait_marker(SPI_MENU_MARK, timeout)
        return t.decode("utf-8", "replace")

    @staticmethod
    def _dump_timeout(capacity_bytes: int) -> float:
        """Raw dump is serial-bottlenecked (~11520 B/s at 115200 baud)."""
        if capacity_bytes > 0:
            return max(120.0, capacity_bytes / RAW_BYTES_PER_SECOND + 120.0)
        return 1800.0

    def spi_flash_dump_raw(
        self,
        output_path: str,
        capacity_bytes: int = 0,
        timeout: float | None = None,
    ) -> dict:
        """Dump RAW (menu 8): raw bytes streamed over the console."""
        if timeout is None:
            timeout = self._dump_timeout(capacity_bytes)
        self._flash_enter()
        self._send_line("8")
        self._wait_marker(b"The raw mode is for python scripting, Continue? [", timeout)
        self._send_line("y")
        t = self._wait_marker(DUMP_DONE_MARK, timeout)
        raw = t[: t.rfind(DUMP_DONE_MARK)]
        if raw.endswith(b"\n"):
            raw = raw[:-1]
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(raw)
        return {"path": str(out), "size": len(raw)}

    def spi_flash_write(
        self,
        address: int,
        data_hex: str | None = None,
        ascii_string: str | None = None,
        auto_erase: bool = True,
        timeout: float = 60.0,
    ) -> str:
        if (data_hex is None) == (ascii_string is None):
            raise ValueError("Provide exactly one of data_hex or ascii_string")
        if data_hex is not None and not data_hex.replace(" ", "").strip():
            raise ValueError("data_hex is empty")
        if data_hex is None:
            assert ascii_string is not None
            data = ascii_string.encode()
        else:
            data = bytes.fromhex(data_hex.replace(" ", ""))
        if not data:
            raise ValueError("empty write data")
        if not (0 <= address < 0x10000000):
            raise ValueError(f"address must be 0-0xFFFFFF, got: {address:#x}")
        self._flash_enter()
        self._send_line("6")
        self._wait_marker(b"Start address (dec or 0x hex) [", timeout)
        self._send_line(f"0x{address:X}")
        self._wait_marker(b"Write an ASCII string? [", timeout)
        if ascii_string is not None:
            self._send_line("y")
            self._wait_marker(b"Enter ASCII string (supports", timeout)
            self._send_line(ascii_string)
        else:
            self._send_line("n")
            self._wait_marker(b"Enter byte values (e.g., 01 A5 FF", timeout)
            self._send_line(" ".join(f"{b:02X}" for b in data))
        self._wait_marker(b"Automatically erase affected blocks", timeout)
        self._send_line("y" if auto_erase else "n")
        self._wait_marker(b"SPI Flash Write: Confirm write operation? [", timeout)
        self._send_line("y")
        t = self._wait_marker(b"SPI Flash Write: Complete.", timeout)
        return t.decode("utf-8", "replace")

    def spi_flash_erase(self, timeout: float = 1800.0) -> str:
        """Erase ENTIRE chip (menu 9). Destructive."""
        self._flash_enter()
        self._send_line("9")
        self._wait_marker(b"SPI Flash Erase: Erase entire flash memory? [", timeout)
        self._send_line("y")
        t = self._wait_marker(b"SPI Flash Erase: Complete.", timeout)
        return t.decode("utf-8", "replace")

    def spi_instruction(self, instruction: str, timeout: float = 15.0) -> bytes:
        """BP6-style instruction syntax, e.g. '[0x9F r:3]' (JEDEC read)."""
        if not 3 <= len(instruction) <= MAX_LINE_CHARS:
            raise ValueError(
                f"instruction must be 3-{MAX_LINE_CHARS} chars, got {len(instruction)}"
            )
        t = self._spi(instruction)
        m = RE_SPI_READ.search(t)
        if not m:
            return b""  # write-only / no read-back
        return bytes.fromhex(" ".join(m.group(1).decode().split()))

    # -- PSU (absent on this board) --

    def set_voltage(self, voltage_v: float, current_limit_ma: int) -> bool:
        raise PSUUnavailableError(
            "This board has no onboard power supply (bare ESP32-S3 devkit). "
            "Power the target from the bench supply."
        )

    def set_power(self, enable: bool) -> bool:
        raise PSUUnavailableError(
            "This board has no onboard power supply (bare ESP32-S3 devkit). "
            "Power the target from the bench supply."
        )

    # -- protocol sessions: phase stubs --

    def configure_uart(self, speed: int = 115200, data_bits: int = 8,
                       parity: bool = False, stop_bits: int = 1) -> None:
        raise NotImplementedError("bitpirate-mcp Phase 4: UART session")

    def read(self) -> bytes:
        raise NotImplementedError("bitpirate-mcp Phase 4: UART session")

    def write(self, data: bytes) -> None:
        raise NotImplementedError("bitpirate-mcp Phase 4: UART session")

    def configure_i2c(self, speed: int = 400000, clock_stretch: bool = False,
                      voltage_mv: int | None = None,
                      current_ma: int | None = None) -> None:
        raise NotImplementedError("bitpirate-mcp Phase 3: I2C session")

    def i2c_transfer(self, write_data: bytes, read_bytes: int = 0) -> bytes:
        raise NotImplementedError("bitpirate-mcp Phase 3: I2C session")

    def i2c_scan(self, start_addr: int = 0x00, end_addr: int = 0x7F) -> list:
        raise NotImplementedError("bitpirate-mcp Phase 3: I2C session")

    def configure_1wire(self, voltage_mv: int | None = None,
                        current_ma: int | None = None) -> None:
        raise NotImplementedError("bitpirate-mcp Phase 3: 1-Wire session")

    def onewire_reset(self):
        raise NotImplementedError("bitpirate-mcp Phase 3: 1-Wire session")

    def onewire_transfer(self, write_data: bytes, read_bytes: int = 0) -> bytes:
        raise NotImplementedError("bitpirate-mcp Phase 3: 1-Wire session")

    # -- teardown --

    def disconnect(self) -> None:
        if self.serial is not None:
            try:
                self._flash_exit(timeout=3.0)
            except Exception:
                pass  # best effort -- board may already be unresponsive
            try:
                self._cmd("mode hiz", timeout=3.0)
            except Exception:
                pass  # best effort -- board may already be unresponsive
            try:
                self.serial.close()
            finally:
                self.serial = None
        self._active_mode = None
