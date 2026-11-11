# bitpirate-mcp

An [MCP](https://modelcontextprotocol.io/) server for the
[ESP32-Bit-Pirate](https://github.com/geo-tp/ESP32-Bit-Pirate) - an open-source,
ESP32-S3-based clone of the Bus Pirate multibus tool. It exposes the bench's
UART, SPI (incl. the on-device flash shell), I2C, 1-Wire and GPIO capabilities
as MCP tools over stdio, so an AI agent can probe, dump and analyze firmware on
a target board end-to-end: attach a device to the ESP32, let the agent drive
the tools, and get extracted binaries and analysis out the other end.

A port of [buspirate-mcp](https://github.com/mplogas/buspirate-mcp)
(Marc Plogas, MIT) from the BusPirate 6's BPIO2 binary protocol to the
Bit-Pirate's text-console protocol. The tool/dispatch/session/safety layers are
largely kept verbatim; the device layer was replaced and re-grounded in the
Bit-Pirate v1.8 firmware source.

## What makes this different from the upstream

| | buspirate-mcp (BP6) | bitpirate-mcp (ESP32-Bit-Pirate) |
|---|---|---|
| Transport | BPIO2 flatbuffers over a 2nd CDC port | Plain text console at 115200 baud (single CDC port) |
| Device layer | `hardware.py` (BPIO2 SDK, submodule) | `bitpirate_hardware.py` (prompt/marker-aware console driver, no vendor deps) |
| Flash work | Manual JEDEC sequences + host-side page program/erase | **Native firmware flash shell**: probe, on-device analysis, search, strings, read, raw dump, patch write, full erase |
| On-device analysis | None | `BinaryAnalyzer` in firmware: file signatures, secret patterns (keys/hashes/URLs), entropy summary |
| Raw transactions | `spi_transfer` byte arrays | BP6-style instruction syntax `[0x9F r:3]` dispatched by the firmware |
| Logic analyzer | FALA (5 tools, 75 MHz, 8 ch) | Removed - no FALA; `verify_connection` uses the DIO `measure` edge/frequency detector |
| Power supply | Hardware PSU (3 tools) | None on the bare devkit - tools report `psu: unavailable` gracefully |
| Signal timing | Fast binary protocol | ~2 s per menu round-trip (firmware main-loop latency); raw dump is serial-bottlenecked (~11.5 KB/s) |

Gains: the Bit-Pirate is *stronger* for RE than the BP6 in one key respect -
the firmware does binary analysis **on the device** (39 secret patterns, file
signature table, entropy), so `spi_flash_analyze` returns findings without
pulling the whole image first. It is also cheaper (~$15 board + devkit vs $100+).

## Architecture

```
MCP client (Claude Code, Hermes, any MCP host)
  |  stdio
  v
server.py            tool definitions + dispatch + safety tiers + session mgmt
  |
  v
tools.py             33 tool implementations (protocol-agnostic where possible)
  |
  v
bitpirate_hardware.py  THE device layer (the only firmware-specific module)
  |  pyserial, /dev/ttyACM0 @115200
  v
ESP32-S3 running ESP32-Bit-Pirate v1.8 text console
```

The console driver has two read modes, both grounded in the firmware's actual
output:

- **Prompt-aware reads** - command, then read until the trailing mode prompt
  (`DIO> `, `SPI> `, …) matches at the *end* of the transcript. Used for mode
  switches, DIO, and instruction-syntax transactions.
- **Marker-aware reads** - shell flows (flash shell, config wizard) don't end
  at a mode prompt; the driver reads until a known completion marker
  (`=== SPI Flash Shell ===`, `SPI Flash Dump: Done.`, `SPI configured.`, …)
  and answers intermediate prompts in the exact order the firmware asks them.

A quadratic-echo line-length guard (250 chars) protects the firmware's
`getLine` input path.

Every command string in the driver was taken from the v1.8 firmware source -
`SpiFlashShell.cpp`, `SpiController.cpp`, `DioController.cpp`,
`UserInputManager.cpp`, `ActionDispatcher.cpp` - not assumed.

## Tools (33)

| Tool | Tier | Notes |
|---|---|---|
| `list_devices` | read-only | USB CDC console ports |
| `verify_connection` | read-only | DIO `measure` edge/frequency detector on TX/RX |
| `scan_baud` | read-only | baud probing over a UART session |
| `open_uart` / `read_output` / `send_command` / `close_uart` | allowed / read | console interaction with targets |
| `set_voltage` / `set_power` | approval | report `psu: unavailable` (no PSU on this board) |
| `enter_download_mode` | allowed | boot/EN pin toggling (0-48 GPIO) |
| `read_flash` | allowed | esptool through the firmware UART **bridge** (Phase 4) |
| `open_spi` / `close_spi` | allowed | mode switch + engagement session; close exits the flash shell |
| `spi_probe` | read-only | firmware flash probe: JEDEC ID, manufacturer, model, capacity, capability notes |
| `spi_read` | read-only | region read as hexdump (flash shell menu 5) |
| `spi_dump` | read-only | **raw full-chip dump to a file** (menu 8) - the workhorse for RE |
| `spi_write` | **approval** | patch write from file/hex/ASCII (menu 6), auto-erase option |
| `spi_flash_erase` | **approval** | full-chip erase (menu 9) - destructive |
| `spi_transfer` | read-only | BP6-style instruction `[0x9F r:3]` (JEDEC, status, custom sequences) |
| `spi_config` | allowed | MOSI/MISO/SCLK/CS pins + 1-80 MHz + WP/HOLD (NVS-persisted) |
| `spi_flash_analyze` | read-only | on-device BinaryAnalyzer: file signatures, secrets, entropy |
| `spi_flash_search` | read-only | string search (1-32 bytes) with context |
| `spi_flash_strings` | read-only | printable-string extraction |
| `open_i2c` / `i2c_scan` / `i2c_read` / `i2c_write` / `i2c_dump` / `close_i2c` | mixed | I2C bus (host-driven; firmware session work pending) |
| `open_1wire` / `onewire_search` / `onewire_read` / `close_1wire` | mixed | 1-Wire bus |

Approval-tier tools require an explicit `_confirmed: true` **and** the
firmware's own confirmation prompts are driven - double-gated for destructive
operations.

## Install

```bash
# any Python 3.10+ (tested on 3.11)
git clone https://github.com/Indigaz/bitpirate-mcp
cd bitpirate-mcp
python3 -m venv .venv          # or: uv venv --python 3.11 .venv
.venv/bin/pip install -e .
```

Register with an MCP host (`.mcp.json`):

```json
{
  "mcpServers": {
    "bitpirate": {
      "command": "/absolute/path/to/bitpirate-mcp/.venv/bin/python",
      "args": ["-m", "buspirate_mcp"]
    }
  }
}
```

Board requirements: an ESP32-S3 devkit running
[ESP32-Bit-Pirate](https://github.com/geo-tp/ESP32-Bit-Pirate) v1.x,
exposing a USB CDC console (`/dev/ttyACM0` on Linux). No pin configuration
needed for first use - the firmware keeps its SPI pin map in NVS
(defaults on a typical Bit-Pirate build: MOSI 40, MISO 39, SCLK 41, CS 4,
20 MHz, WP 7); `spi_config` re-points any free GPIO.

## Typical RE workflow

1. Attach a target to the bench (SPI flash via clip, or I2C bus).
2. `spi_probe` - identify the chip (ID, manufacturer, capacity).
3. `spi_dump` → `~/re-lab/<target>/flash.bin` - raw image off the bus.
4. `spi_flash_analyze` / `spi_flash_strings` / `spi_flash_search` - on-device
   findings (what firmware, embedded keys, embedded binaries).
5. Hand the image to your static-analysis tooling (Ghidra, REA, etc.).

## Testing & verification

- **221 tests**, all passing. The device layer is tested with a
  **transcript-driven protocol mock** (`FakeSerial`): scripted firmware
  transcripts are released only after the triggering command is written,
  simulating real round-trip latency so command/response pairing cannot bleed
  across commands. The mock is a FIFO of (line → response) so repeated prompts
  (e.g. `y`/`y`) resolve in firmware order, and it accepts raw bytes for the
  dump path.
- **Live-verified on hardware** (ESP32-S3 devkit + Bit-Pirate v1.8, no target
  attached - no-chip paths):
  - DIO: read, edge counter, and electrical drive-HIGH/LOW self-test
  - instruction syntax: `[0x9F r:3]` → `00 00 00` (floating bus, correct)
  - flash shell: probe (no-chip detection), analyze short-circuit, clean shell exit
- Reproduce the live smoke tests: `scripts/smoke_live.py`,
  `scripts/smoke_live_spi.py` (read-only by design).

## Roadmap

- **Phase 3** - I2C/1-Wire sessions rewritten against the firmware's native
  controllers (scan/read/write/dump + I2C monitor/glitch when grounded);
  SPI **EEPROM shell** (24cxx/25xxx - matches common bench EEPROMs).
- **Phase 4** - `read_flash` via the firmware UART bridge + esptool
  (transparent bridge is persistent; requires a button/USB-replug to exit).
- **Phase 5** - packaging: rename the `buspirate_mcp` package to
  `bitpirate_mcp`, PyPI publish, CI.

## Credits

- **Upstream**: [buspirate-mcp](https://github.com/mplogas/buspirate-mcp) by
  [Marc Plogas](https://github.com/mplogas) (MIT) - the tool/dispatch/session/
  safety architecture it builds on.
- **Hardware & firmware**: [ESP32-Bit-Pirate](https://github.com/geo-tp/ESP32-Bit-Pirate)
  by geo-tp (the console protocol, flash shell and analyzer this port drives).
- **Port to the ESP32-Bit-Pirate**: designed and implemented by
  [Hermes Agent](https://github.com/NousResearch/hermes-agent) (Nous Research)
  - the LLM agent driving this bench - under direction by
  [Christian Brooks](https://github.com/Indigaz) (Indigaz), who owns the
  hardware, set the goals, and reviews the work.
- **Method**: every firmware-facing string is grounded in v1.8 source; the
  whole port is test-first (221 tests) and smoke-tested against the real board
  before each phase ships.

## License

MIT - see [LICENSE](LICENSE). Upstream copyright (c) Marc Plogas;
port copyright (c) 2026 Hermes Agent (Nous Research) / Christian Brooks.
