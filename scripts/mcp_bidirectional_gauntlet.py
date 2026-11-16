#!/usr/bin/env python3
"""Two-board bidirectional MCP gauntlet.

Drives the *real* bitpirate-mcp dispatcher (server.call_tool) against board A
(XIAO ESP32S3 running BitPirate, /dev/ttyACM0) while board B (ESP32-S3-DevKitC-1
running the SPI-slave responder) sits on the wired SPI link. Proves real
bidirectional data (host -> MCP -> A -> wire -> B -> wire -> A -> MCP -> host)
and honestly surfaces the Phase 3/4 stub tools.

Usage:
    .venv/bin/python scripts/mcp_bidirectional_gauntlet.py
"""
from __future__ import annotations

import asyncio
import json
import sys

import buspirate_mcp.server as server
from buspirate_mcp.bitpirate_hardware import BitPirateHardware

A_PORT = "/dev/ttyACM0"

# Board A SPI pin map (matches the wired link + xiao-esp32s3 env):
#   D10=GPIO9 MOSI, D9=GPIO8 MISO, D8=GPIO7 SCK, D3=GPIO4 CS
SPI_PINS = {"mosi": 9, "miso": 8, "sclk": 7, "cs": 4}


async def call(name: str, args: dict) -> dict:
    try:
        contents = await server.call_tool(name, args)
        return json.loads(contents[0].text)
    except Exception as exc:  # defensive: dispatcher already catches, but be safe
        return {"error": f"{type(exc).__name__}: {exc}", "tool": name}


async def main() -> int:
    hw = BitPirateHardware.connect(A_PORT)
    server._hardware = hw
    server._hardware_port = A_PORT

    out: dict = {"port": A_PORT, "steps": [], "bidirectional_ok": False}

    def record(step: str, name: str, data: dict, ok: bool, note: str = "") -> None:
        entry = {"step": step, "tool": name, "ok": ok, "note": note, "result": data}
        out["steps"].append(entry)
        marker = "PASS" if ok else "FAIL"
        print(f"[{marker}] {step}: {name}" + (f"  -- {note}" if note else ""))
        print(json.dumps(data, indent=2)[:900])
        print()

    # 1. Discovery
    d = await call("list_devices", {})
    record("discover", "list_devices", d, "devices" in d and len(d["devices"]) >= 1,
           f"{len(d.get('devices', []))} console port(s)")

    # 2. DIO activity on the SPI data lines (B is idle -> expect low/no edges)
    d = await call("verify_connection",
                   {"tx_pin": SPI_PINS["mosi"], "rx_pin": SPI_PINS["miso"],
                    "sample_duration_ms": 2000})
    record("dio-activity", "verify_connection", d,
           "activity_detected" in d,
           f"activity={d.get('activity_detected')} transitions={d.get('transitions')}")

    # 3. Open SPI
    d = await call("open_spi", {"engagement_name": "boardB-spi"})
    session = d.get("session_id")
    record("open-spi", "open_spi", d, session is not None, f"session={session}")

    # 4. Configure SPI pin map + clock
    d = await call("spi_config", {**SPI_PINS, "freq_mhz": 2})
    record("spi-config", "spi_config", d, d.get("configured") is True,
           f"{SPI_PINS} @2MHz")

    # 5. BIDIRECTIONAL: RDID (A -> B 0x9F, B -> A 12 34 56).
    #    [0x9F D:2 r:3] clocks exactly 4 bytes -> aligns with the ESP32
    #    slave's 32-bit (4-byte) word transfer.
    d = await call("spi_transfer", {"instruction": "[0x9F D:2 r:3]"})
    rx = d.get("rx_hex", "")
    bid = (rx == "123456")
    out["bidirectional_ok"] = bid
    record("bidirectional-rdid", "spi_transfer [0x9F D:2 r:3]", d, bid,
           f"rx_hex={rx!r} (expect '123456')")

    # 6. BIDIRECTIONAL repeat: same RDID again -> reliable link.
    d = await call("spi_transfer", {"instruction": "[0x9F D:2 r:3]"})
    record("bidirectional-rdid-repeat", "spi_transfer [0x9F D:2 r:3]", d,
           d.get("rx_hex") == "123456", f"rx_hex={d.get('rx_hex')!r} (expect '123456')")

    # 7. BIDIRECTIONAL flow: a default command. NOTE: the ESP32 slave emits its
    #    response in the SAME 4-byte word it receives the command, so the
    #    response lags the command by one round (off-by-one). We only assert
    #    that 3 bytes flow back, not their value.
    d = await call("spi_transfer", {"instruction": "[0xAB D:2 r:3]"})
    flow_ok = (d.get("rx_hex") is not None and len(d.get("rx_hex", "")) == 6)
    record("bidirectional-flow", "spi_transfer [0xAB D:2 r:3]", d, flow_ok,
           f"rx_hex={d.get('rx_hex')!r} (3 bytes flow back; value is the prior preset due to 1-round lag)")

    # 8. Flash-shell probe (back-to-back bytes; bonus, may be flaky vs a non-flash slave)
    d = await call("spi_probe", {})
    record("flash-probe", "spi_probe", d, "present" in d,
           f"present={d.get('present')} id={d.get('id')}")

    # 9. Close SPI
    d = await call("close_spi", {"session_id": session}) if session else {"error": "no session"}
    record("close-spi", "close_spi", d, d.get("closed") is True)

    # --- Phase 3/4 stubs (honest coverage report) ---
    d = await call("open_uart", {"baud": 115200, "engagement_name": "stub"})
    record("stub-uart", "open_uart", d, "error" in d,
           f"{d.get('error', 'no error')}")

    d = await call("open_i2c", {"engagement_name": "stub"})
    record("stub-i2c", "open_i2c", d, "error" in d,
           f"{d.get('error', 'no error')}")

    d = await call("open_1wire", {"engagement_name": "stub"})
    record("stub-1wire", "open_1wire", d, "error" in d,
           f"{d.get('error', 'no error')}")

    # 10. Safety gate: APPROVAL_WRITE tools require _confirmed
    d = await call("set_voltage", {"voltage_v": 3.3, "current_limit_ma": 100})
    record("safety-gate", "set_voltage (no _confirmed)", d,
           d.get("confirmation_required") is True,
           "approval gate held as expected")

    # 11. With confirmation -> board has no PSU
    d = await call("set_voltage", {"voltage_v": 3.3, "current_limit_ma": 100,
                                   "_confirmed": True})
    record("psu-absent", "set_voltage (_confirmed=True)", d, "error" in d,
           f"{d.get('error', 'no error')}")

    passed = sum(1 for s in out["steps"] if s["ok"])
    out["summary"] = {"passed": passed, "total": len(out["steps"]),
                      "bidirectional_ok": out["bidirectional_ok"]}
    print("=" * 60)
    print(f"SUMMARY: {passed}/{len(out['steps'])} steps passed; "
          f"BIDIRECTIONAL={'OK' if out['bidirectional_ok'] else 'FAILED'}")
    print(json.dumps(out["summary"], indent=2))
    with open("/home/christian/.hermes/cache/scratch/mcp_gauntlet_result.json", "w") as f:
        json.dump(out, f, indent=2)
    return 0 if out["bidirectional_ok"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
