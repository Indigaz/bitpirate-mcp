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
        """Un-park 'Press any key to start' if present; drain the banner."""
        s = self._require_serial()
        s.write(b" \r")
        return self._read_quiescent(timeout=WAKE_TIMEOUT, quiet=WAKE_QUIET)

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

    def configure_spi(self, speed: int = 1000000, clock_polarity: bool = False,
                      clock_phase: bool = False, chip_select_idle: bool = True,
                      voltage_mv: int | None = None,
                      current_ma: int | None = None) -> None:
        raise NotImplementedError("bitpirate-mcp Phase 2: SPI session")

    def spi_transfer(self, write_data: bytes, read_bytes: int = 0) -> bytes:
        raise NotImplementedError("bitpirate-mcp Phase 2: SPI session")

    def spi_select(self) -> None:
        raise NotImplementedError("bitpirate-mcp Phase 2: SPI session")

    def spi_deselect(self) -> None:
        raise NotImplementedError("bitpirate-mcp Phase 2: SPI session")

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
                self._cmd("mode hiz", timeout=3.0)
            except Exception:
                pass  # best effort -- board may already be unresponsive
            try:
                self.serial.close()
            finally:
                self.serial = None
        self._active_mode = None
