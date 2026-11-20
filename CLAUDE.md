# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

bitpirate-mcp is an MCP server for the ESP32-Bit-Pirate (ESP32-S3 devkit
running geo-tp/ESP32-Bit-Pirate v1.x firmware). It drives the firmware's text
console over USB CDC (/dev/ttyACM0 @115200) and exposes UART, SPI (native
flash shell), I2C, 1-Wire, and GPIO operations as MCP tools over stdio.
It is a port of mplogas/buspirate-mcp (BPIO2 protocol) to this board.

## Architecture

```
MCP client (stdio)
  |
server.py            tool defs + dispatch + safety tiers + sessions
  |
tools.py             tool implementations
  |
bitpirate_hardware.py  THE device layer (only firmware-specific module)
  |  pyserial
  v
ESP32-S3 text console (ESP32-Bit-Pirate v1.8)
```

Only `bitpirate_hardware.py` talks to the serial port. Everything else calls
its methods. This makes mocking trivial for tests.

### Console transport: two read modes

Firmware flows do not all end the same way:

- **Prompt-aware** (`_cmd` / `_read_until_prompt`): command, then read until a
  mode prompt (`DIO> `, `SPI> `, ...) matches at the END of the transcript.
  Prompt regex is `[A-Z0-9]{2,10}>[ \t]*\r?\s*$` (digits matter: `I2C>`).
  Used for mode switches, DIO commands, instruction syntax.
- **Marker-aware** (`_wait_marker`): flash-shell and config-wizard flows end
  at completion markers (`=== SPI Flash Shell ===`, `SPI Flash Dump: Done.`,
  `SPI configured.`, ...), not prompts. The driver answers intermediate
  prompts in firmware order: choice prompts are 1-indexed with empty=default,
  number prompts `label [def]: `, yes/no `label [y]: `/`[n]: `.

Firmware error markers in a transcript raise `BitPirateCLIError` with the
transcript attached. Line length is capped at 250 chars (quadratic-echo guard
in the firmware's getLine).

### Flash shell (SPI mode)

`flash` opens a persistent menu shell (1 Probe, 2 Analyze, 3 Search, 4
Strings, 5 Read, 6 Write, 7 Dump hex, 8 Dump RAW, 9 Erase, 10 Exit). The
driver tracks `_flash_shell_open` and reuses the shell across calls;
`close_spi` / `disconnect` exit it. Menu 8 streams raw bytes over the
console (serial-bottlenecked ~11.5 KB/s; `_dump_timeout` computes the
deadline from capacity). Raw dump ends at the `SPI Flash Dump: Done.`
sentinel, which the driver strips from the file.

Instruction syntax `[0x9F r:3]` is dispatched by the firmware to the active
mode's `handleInstruction`; SPI results print `SPI Read:` + uppercase hex
`%02X ` per byte. Write-only instructions print nothing (returns b"").

### Grounding rule

Every command string, prompt marker, and completion marker in the driver
comes from the v1.8 firmware source (SpiFlashShell.cpp, SpiController.cpp,
DioController.cpp, UserInputManager.cpp, ActionDispatcher.cpp). Do not
"improve" a command string from memory - check the firmware source first.

## Safety Model

Three tiers enforced at the MCP server boundary (safety.py):

- **read-only**: list_devices, verify_connection, scan_baud, read_output,
  spi_probe, spi_read, spi_dump, spi_transfer, spi_flash_analyze,
  spi_flash_search, spi_flash_strings, i2c_scan, i2c_read, i2c_dump, i2c_ping,
  onewire_search, onewire_read, onewire_ping, onewire_temp,
  measure_pin, measure_frequency, capture, read_pin
- **allowed-write**: open_uart, send_command, close_uart, enter_download_mode,
  read_flash, open_spi, close_spi, spi_config, open_i2c, close_i2c,
  open_i2c_slave, close_i2c_slave, i2c_slave_log, open_1wire, close_1wire,
  onewire_write_spad, set_pin, pulse, pull_up, pull_down, pwm_set, pwm_stop,
  servo_set
- **approval-write**: set_voltage, set_power, spi_write, i2c_write,
  spi_flash_erase - require literal `_confirmed: true` AND the firmware's own
  confirmation prompt is driven. Do not bypass or weaken either gate.

## Board Notes

- Console port: /dev/ttyACM0 (user needs `dialout` group).
- No PSU on this board: set_voltage/set_power raise PSUUnavailableError;
  tools return `psu: unavailable`. Power the target from the bench supply.
- No FALA logic analyzer: verify_connection uses DIO `measure`
  (edge counter + approximate frequency), not ADC voltage.
- S3 strapping/reserved pins are rejected by `_validate_pin` (3, 19, 20,
  22-37, 45, 46).
- SPI pin map is NVS-persisted by the firmware; `spi_config` re-points it
  (7-prompt walk). Typical Bit-Pirate defaults: MOSI 40, MISO 39, SCLK 41,
  CS 4, 20 MHz, WP 7.
- `bridge` (UART passthrough for esptool) is persistent: exit requires the
  physical button or a USB replug.

## Build and Run

```bash
python3 -m venv .venv            # or: uv venv --python 3.11 .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m buspirate_mcp          # stdio server (spawned by MCP client)
.venv/bin/python -m pytest                 # 287 tests, no hardware needed
.venv/bin/python scripts/smoke_live.py     # live DIO smoke test (board attached)
.venv/bin/python scripts/smoke_live_spi.py # live SPI shell smoke test (read-only)
.venv/bin/python scripts/protocol_battery.py # 92-step end-to-end battery (2-board rig)
```

## Style

- Python 3.11+ (mcp pinned <2.0; 2.x removed the legacy Server API)
- No emojis, no em-dashes in code, comments, commits, or docs
- Commit messages: short, to the point
- Tests: transcript-driven (FakeSerial protocol mock in
  tests/test_bitpirate_hardware.py, imported by the SPI test module). The
  mock is a FIFO of (line, response) pairs; responses release only after the
  triggering line is written; raw bytes are supported for the dump path.
