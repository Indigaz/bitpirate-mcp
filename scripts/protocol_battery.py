#!/usr/bin/env python3
"""Multi-protocol battery for the two-board rig.

Boards:
  A = XIAO ESP32S3 (BitPirate v1.8), console /dev/ttyACM0 @115200 (USB-CDC, HIZ> prompt)
  B = ESP32-S3-DevKitC-1 (rig-responder v1.3), console /dev/ttyACM1 @115200 (UART0 via CH340)

Wired links (all 3.3 V):
  A43(TX)  -> B4   (UART1 RX)
  A44(RX)  <- B15  (UART1 TX / GPIO mirror out)
  A5(SDA)  <-> B8  (I2C slave SDA)
  A6(SCL)  <-> B9  (I2C slave SCL)
  A9(D10)  <-> B11 (1-Wire DQ)
  A4(D3)   <-> B10 (DIO / GPIO mirror in)
  GND <-> GND

Tiers:
  CONSOLE  - drives A's real firmware console (mode uart / dio / i2c / 1wire).
             The MCP stubs these protocols, so the console is where the real
             firmware-level test happens.
  MCP      - drives the real bitpirate-mcp dispatcher (server.call_tool):
             DIO (real), download-mode sequence (real DIO path), PSU tools
             (hardware absent), and the 12 UART/I2C/1-Wire stub tools.

Usage:
  .venv/bin/python scripts/protocol_battery.py
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import threading
import time

import serial

import buspirate_mcp.server as server
from buspirate_mcp.bitpirate_hardware import BitPirateHardware

A_PORT = "/dev/ttyACM0"
B_PORT = "/dev/ttyACM1"
BAUD = 115200

# Board A pin map (wired links)
UART_RX, UART_TX = 44, 43
I2C_SDA, I2C_SCL = 5, 6
DW_PIN = 9
DIO_DRIVE, DIO_READ = 4, 44

RESULT_PATH = "/home/christian/.hermes/cache/scratch/protocol_battery_result.json"

# hex line payloads for A's `write hex{...}` (byte value -> hex)
PING_NL = "50 49 4E 47 0A"     # "PING\n"
HELLO_NL = "48 45 4C 4C 4F 0A"  # "HELLO\n"
GPIO_NL = "47 50 49 4F 0A"      # "GPIO\n"
SQUARE_NL = "53 51 55 41 52 45 0A"  # "SQUARE\n"
STOP_NL = "53 54 4F 50 0A"      # "STOP\n"

ROM_EXPECTED = "28 11 22 33 44 55 66"  # B's 1-Wire ROM (last byte = CRC, any value)


class Console:
    """Tiny driver for A's serial console (command -> marker wait)."""

    def __init__(self, port: str) -> None:
        self.ser = serial.Serial(port, BAUD, timeout=1)
        self.ser.reset_input_buffer()

    def send(self, text: str) -> None:
        self.ser.write((text + "\r").encode("ascii"))

    def drain(self, secs: float) -> str:
        t0 = time.time()
        out = b""
        while time.time() - t0 < secs:
            d = self.ser.read(self.ser.in_waiting or 0)
            if d:
                out += d
            else:
                time.sleep(0.01)
        return out.decode("utf-8", "replace")

    def wait(self, marker: str, timeout: float = 12.0, settle: float = 0.35) -> tuple[bool, str]:
        t0 = time.time()
        out = b""
        while time.time() - t0 < timeout:
            out += self.ser.read(self.ser.in_waiting or 0)
            if marker in out.decode("utf-8", "replace"):
                out += self.drain(settle).encode("utf-8")
                return True, out.decode("utf-8", "replace")
            else:
                time.sleep(0.01)
        return False, out.decode("utf-8", "replace")

    def cmd(self, text: str, marker: str | None = None, timeout: float = 12.0,
            settle: float = 0.35) -> tuple[bool, str]:
        self.ser.reset_input_buffer()
        self.send(text)
        if marker is None:
            return True, self.drain(timeout)
        return self.wait(marker, timeout, settle)

    def close(self) -> None:
        try:
            self.ser.close()
        except Exception:
            pass


class BLog:
    """Background capture of B's console (evidence of B-side activity)."""

    def __init__(self, port: str, do_reset: bool = True) -> None:
        self.ser = serial.Serial(port, BAUD, timeout=0.5)
        self.buf: list[str] = []
        self.lock = threading.Lock()
        self.alive = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        if do_reset:
            self.reset_board()

    def reset_board(self) -> None:
        """esptool-style reset: B boots fresh (u1 up, mirror off, banner captured)."""
        self.ser.dtr = False
        self.ser.rts = False
        time.sleep(0.05)
        self.ser.dtr = True
        time.sleep(0.1)
        self.ser.rts = True
        time.sleep(1.5)  # banner + ROM/SPAD lines land in the reader thread

    def write(self, text: str) -> None:
        with self.lock:
            self.ser.write((text + "\r").encode("ascii"))

    def _run(self) -> None:
        while self.alive:
            d = self.ser.read(64)
            if d:
                with self.lock:
                    self.buf.append(d.decode("utf-8", "replace"))
                    if sum(len(x) for x in self.buf) > 50000:
                        del self.buf[: max(1, len(self.buf) // 2)]

    def snapshot(self) -> str:
        with self.lock:
            return "".join(self.buf)

    def close(self) -> None:
        self.alive = False
        try:
            self.ser.close()
        except Exception:
            pass


async def mcp_call(name: str, args: dict) -> dict:
    try:
        contents = await server.call_tool(name, args)
        return json.loads(contents[0].text)
    except Exception as exc:  # defensive; dispatcher already catches
        return {"error": f"{type(exc).__name__}: {exc}", "tool": name}


class Battery:
    def __init__(self) -> None:
        self.steps: list[dict] = []

    def record(self, step: str, tier: str, ok: bool, detail: str = "", raw: str = "",
               known: bool = False) -> None:
        entry = {"step": step, "tier": tier, "ok": ok, "detail": detail,
                 "raw": raw[-600:], "known": known}
        self.steps.append(entry)
        marker = "KNOWN" if known else ("PASS" if ok else "FAIL")
        print(f"[{marker}] {step} ({tier})" + (f"  -- {detail}" if detail else ""))

    def summary(self) -> dict:
        # "known" steps (documented limitations) are reported but excluded
        # from the pass/total score.
        counted = [s for s in self.steps if not s.get("known")]
        known = [s for s in self.steps if s.get("known")]
        passed = sum(1 for s in counted if s["ok"])
        by_tier: dict[str, dict[str, int]] = {}
        for s in counted:
            t = by_tier.setdefault(s["tier"], {"pass": 0, "total": 0, "known": 0})
            t["total"] += 1
            if s["ok"]:
                t["pass"] += 1
        for s in known:
            by_tier.setdefault(s["tier"], {"pass": 0, "total": 0, "known": 0})["known"] += 1
        return {"passed": passed, "total": len(counted), "known": len(known),
                "by_tier": by_tier}


def run_console_tier(bat: Battery, con: Console, blog: BLog) -> None:
    T = "console"

    # ---------- preflight (forced HIZ + prompt recovery) ----------
    # A may be left mid-prompt from a prior run (an interactive [y]: or an
    # I2C/UART config prompt). Flush pending input, then force HIZ. Retry a
    # couple of times so a clean prompt is guaranteed before the battery.
    ok = False
    txt = ""
    for _attempt in range(3):
        con.ser.reset_input_buffer()
        con.send("")               # newline: settle/abort any pending prompt
        con.drain(0.4)
        ok, txt = con.cmd("mode hiz", "HIZ>", 6)
        if ok:
            break
    bat.record("pre-a-console-live", T, ok,
               "A answers at HIZ>" if ok else "no HIZ> prompt after recovery", txt)

    # ---------- wiring audit (ground truth from B's pin monitor) ----------
    # A toggles each wired pin H/L/H/L (250 ms each) while B logs levels of
    # pins 4/8/9/10/11/15; B reports per-pin transition counts.
    blog.write("AUDIT 8")
    time.sleep(0.3)
    con.cmd("mode dio", "DIO>", 4)
    for pin in (43, 5, 6, 4, 9):
        for lvl in ("H", "L", "H", "L"):
            con.send(f"set {pin} {lvl}")
            time.sleep(0.25)
            con.drain(0.02)
    con.cmd("mode hiz", "HIZ>", 4)
    t0 = time.time()
    audit_txt = ""
    while time.time() - t0 < 12:
        audit_txt = blog.snapshot()
        if "AUDIT end:" in audit_txt:
            break
        time.sleep(0.1)
    m = re.search(r"AUDIT end:.*", audit_txt)
    audit_line = m.group(0).strip() if m else ""
    bat.record("audit-wiring", T, bool(audit_line),
               audit_line or "no AUDIT end on B console", "")

    # ---------- UART tier ----------
    ok, txt = con.cmd("mode uart", "UART>", 4)
    bat.record("uart-mode", T, ok, "", txt)

    # config: RX, TX, baud, data bits, parity, stop bits, inverted?
    con.send("config")
    for a in [str(UART_RX), str(UART_TX), "115200", "8", "N", "1", "n"]:
        time.sleep(0.15)
        con.send(a)
    ok, txt = con.wait("onfigured.", 15)
    bat.record("uart-config", T, ok,
               f"RX={UART_RX} TX={UART_TX} 115200 8N1", txt)

    ok, txt = con.cmd(f"write hex{{{PING_NL}}}", "UART Write:", 4)
    bat.record("uart-write-ping", T, ok and "sent 5 bytes" in txt,
               "A sent PING\\n (5 bytes)" if ok else "no write confirmation", txt)

    con.send("read")
    stream = con.drain(1.2)          # B sends PONG x10 over 500ms; A's read flushes at start
    con.send("")                     # Enter stops streaming
    ok, txt = con.wait("Stopped", 5)
    pong = "PONG" in (stream + txt)
    bat.record("uart-read-pong", T, ok and pong,
               f"PONG count={ (stream + txt).count('PONG') }" if pong else f"no PONG; stream={stream!r}", stream + txt)

    ok, txt = con.cmd(f"write hex{{{HELLO_NL}}}", "UART Write:", 4)
    con.send("read")
    stream = con.drain(1.0)          # B echoes ECHO:HELLO x3 over 150ms
    con.send("")
    ok2, txt2 = con.wait("Stopped", 5)
    echo = "ECHO:HELLO" in (stream + txt2)
    bat.record("uart-echo-hello", T, ok and ok2 and echo,
               "A sent HELLO\\n, B echoed ECHO:HELLO" if echo else f"no echo; stream={stream!r}",
               stream + txt2)

    ok, txt = con.cmd("ping", "Device detected", 12)
    bat.record("uart-ping-detect", T, ok,
               "A probed 5s, B's echo made >=5 printable chars", txt)

    # ---------- DIO tier ----------
    # SQUARE/STOP go via B's OWN console: pin 15 is owned by u1 (UART1 TX)
    # until u1.end(), so B cannot GPIO-drive it while UART1 is up (v1.3 bug:
    # digitalWrite(15) left the pin on the UART TX matrix, stuck idle HIGH).
    ok, txt = con.cmd("mode dio", "DIO>", 4)
    bat.record("dio-mode", T, ok, "", txt)

    blog.write("SQUARE")
    time.sleep(0.3)
    bat.record("dio-square-arm", T, "SQUARE 1kHz" in blog.snapshot(),
               "B square wave armed via B console (u1 ended)", "")

    ok, txt = con.cmd(f"measure {DIO_READ} 1000", "Rising edges", 6)
    m = re.search(r"Rising edges:\s+(\d+)", txt)
    rising = int(m.group(1)) if m else -1
    bat.record("dio-measure-edges", T, ok and rising >= 300,
               f"rising edges in 1s = {rising} (expect ~500 at 1 kHz)", txt)

    blog.write("STOP")
    time.sleep(0.3)
    bat.record("dio-square-stop", T, "STOP (out=15 low" in blog.snapshot(),
               "B square wave stopped, u1 restored", "")

    # Arm B's GPIO mirror via A's UART (last: ends B's UART1 permanently;
    # mirror stays armed for the MCP tier).
    ok, txt = con.cmd("mode uart", "UART>", 4)
    ok2, txt2 = con.cmd(f"write hex{{{GPIO_NL}}}", "UART Write:", 4)
    time.sleep(0.4)
    arm = "MODE=GPIO" in blog.snapshot()
    bat.record("dio-mirror-arm", T, ok and ok2 and arm,
               "B switched to GPIO mirror mode" if arm else "no MODE=GPIO on B console", txt2)

    ok, txt = con.cmd("mode dio", "DIO>", 4)
    ok2, txt2 = con.cmd(f"set {DIO_DRIVE} H", None, 0.5)
    ok3, txt3 = con.cmd(f"read {DIO_READ}", "GPIO 44", 4)
    hi = ok3 and "= 1" in txt3
    bat.record("dio-high", T, hi,
               f"set {DIO_DRIVE} H -> read {DIO_READ} (B mirror loopback)" if hi else txt3[-120:], txt3)

    ok, txt = con.cmd(f"set {DIO_DRIVE} L", None, 0.5)
    ok2, txt2 = con.cmd(f"read {DIO_READ}", "GPIO 44", 4)
    lo = ok2 and "= 0" in txt2
    bat.record("dio-low", T, lo,
               f"set {DIO_DRIVE} L -> read {DIO_READ}" if lo else txt2[-120:], txt2)

    ok, txt = con.cmd("mode hiz", "HIZ>", 4)
    bat.record("back-to-hiz", T, ok, "mirror stays armed for MCP tier", txt)

    # ---------- I2C tier ----------
    # A's I2C mode entry prints an interactive config prompt (unlike UART/DIO).
    ok, txt = con.cmd("mode i2c", "SDA GPIO", 4)
    for a in [str(I2C_SDA), str(I2C_SCL), "100000"]:
        time.sleep(0.15)
        con.send(a)
    ok2, txt2 = con.wait("onfigured.", 15)
    bat.record("i2c-mode", T, ok and ok2,
               f"SDA={I2C_SDA} SCL={I2C_SCL} 100 kHz (config at mode entry)", txt2[-200:])

    ok, txt = con.cmd("ping 0x50", "ACK received", 4)
    bat.record("i2c-ping", T, ok, "B I2C slave ACKed at 0x50", txt)

    ok, txt = con.cmd("scan", "Found device", 8)
    bat.record("i2c-scan", T, ok and "0x50" in txt,
               "scan found 0x50" if ok and "0x50" in txt else txt[-150:], txt)

    # A's `write` asks an interactive "[y]:" confirmation before executing.
    con.send("write 0x50 3 A5")
    ok, txt = con.wait("[y]:", 4)
    con.send("y")
    ok2, txt2 = con.wait("I2C Write:", 4)
    ok = ok and ok2 and "Aborting" not in txt2 and "no response" not in txt2
    bat.record("i2c-write-03-a5", T, ok, "reg 3 <- A5", txt2)

    ok, txt = con.cmd("read 0x50 3", "I2C Read:", 4)
    bat.record("i2c-read-03-a5", T, ok and "A5" in txt.upper(),
               f"reg 3 read back: {txt[-80:]}" if ok else txt[-120:], txt)

    con.send("write 0x50 7 42")
    ok, txt = con.wait("[y]:", 4)
    con.send("y")
    ok2, txt2 = con.wait("I2C Write:", 4)
    ok = ok and ok2 and "Aborting" not in txt2
    bat.record("i2c-write-07-42", T, ok, "reg 7 <- 42", txt2)

    ok, txt = con.cmd("read 0x50 7", "I2C Read:", 4)
    bat.record("i2c-read-07-42", T, ok and "42" in txt.upper(),
               f"reg 7 read back: {txt[-80:]}" if ok else txt[-120:], txt)

    ok, txt = con.cmd("read 0x50 0", "I2C Read:", 4)
    bat.record("i2c-read-00-zero", T, ok and "00" in txt.upper(),
               "unwritten reg 0 reads 00", txt)

    # ---------- 1-Wire tier ----------
    # clean mode boundary: A may still be mid-prompt after the I2C tier
    con.cmd("mode hiz", "HIZ>", 4)
    ok, txt = con.cmd("mode 1wire", "1WIRE>", 4)
    bat.record("1wire-mode", T, ok, "", txt)

    con.send("config")
    time.sleep(0.15)
    con.send(str(DW_PIN))
    ok, txt = con.wait("onfigured.", 15)
    bat.record("1wire-config", T, ok, f"DQ={DW_PIN}", txt)

    ok, txt = con.cmd("ping", "Device present", 8)
    bat.record("1wire-ping", T, ok,
               "presence pulse from B" if ok else "no presence (add 4.7k pull-up?)", txt)

    ok, txt = con.cmd("read", "OneWire Read: Complete.", 10)
    rom_ok = ROM_EXPECTED in txt
    spad_ok = "Scratchpad:" in txt
    bat.record("1wire-read", T, ok and rom_ok and spad_ok,
               "ROM + scratchpad" if ok and rom_ok else f"ROM match={rom_ok}", txt)

    ok, txt = con.cmd("scan", "Device 1", 10)
    if not ok:
        time.sleep(1.0)
        txt = con.drain(1.0)
    bat.record("1wire-scan", T, ok and ROM_EXPECTED[:2] in txt,
               "search found device" if ok else "no device found in search", txt)

    # WRITE SP: KNOWN B-side responder limitation (documented 2026-11-15).
    # A drives correct bits on the wire (verified by DQ waveform: byte1=0xCC,
    # byte2=0x0F both clean), but the responder misreads the consecutive
    # write-sp bytes (reads 0x0F as 0xCF). Reported as KNOWN — visible in the
    # output but excluded from the pass/total score.
    ok, txt = con.cmd("write sp 11 22 33 44 55 66 77 52", "OneWire Write:", 20)
    wsp_ok = ("Failed" not in txt) and ("Mismatch" not in txt)
    bat.record("1wire-write-sp", T, wsp_ok,
               "write-sp passed (limitation no longer reproduces)" if wsp_ok
               else "KNOWN B-side limitation: responder misreads consecutive "
                    "write-sp bytes (A drives correct bits on the wire)",
               txt, known=True)


async def run_mcp_tier(bat: Battery, blog: BLog) -> None:
    T = "mcp"

    d = await mcp_call("list_devices", {})
    devs = d.get("devices", [])
    bat.record("m1-list-devices", T, len(devs) >= 2,
               f"{len(devs)} console ports found", json.dumps(d)[:300])

    d = await mcp_call("verify_connection", {"tx_pin": 9, "rx_pin": 8,
                                             "sample_duration_ms": 2000})
    bat.record("m2-verify-connection", T, "activity_detected" in d,
               f"activity={d.get('activity_detected')} transitions={d.get('transitions')}",
               json.dumps(d)[:300])

    pre = blog.snapshot()
    d = await mcp_call("enter_download_mode", {"boot_pin": 2, "reset_pin": 4})
    post = blog.snapshot()
    fresh = post[len(pre):] if post.startswith(pre) else post
    seq_ok = (d.get("status") == "download_mode_entered")
    obs = ("MIRROR in=0" in fresh) and ("MIRROR in=1" in fresh)
    bat.record("m3-enter-download-mode", T, seq_ok and obs,
               "DIO sequence executed; B mirror saw reset pin low->high"
               if obs else "B mirror did not log transitions in this sequence", json.dumps(d)[:300])

    d = await mcp_call("set_voltage", {"voltage_v": 3.3, "current_limit_ma": 100})
    bat.record("m4-psu-safety-gate", T, d.get("confirmation_required") is True,
               "APPROVAL_WRITE gate held without _confirmed", json.dumps(d)[:300])

    d = await mcp_call("set_voltage", {"voltage_v": 3.3, "current_limit_ma": 100,
                                       "_confirmed": True})
    bat.record("m5-psu-voltage-absent", T, "error" in d,
               d.get("error", "no error")[:120], json.dumps(d)[:300])

    d = await mcp_call("set_power", {"enable": True, "_confirmed": True})
    bat.record("m6-psu-power-absent", T, "error" in d,
               d.get("error", "no error")[:120], json.dumps(d)[:300])

    stubs = [
        ("open_uart", {"baud": 115200, "engagement_name": "battery"}),
        ("scan_baud", {}),
        ("read_output", {"session_id": "stub"}),
        ("send_command", {"session_id": "stub", "command": "AT"}),
        ("open_i2c", {"engagement_name": "battery"}),
        ("i2c_scan", {"session_id": "stub"}),
        ("i2c_read", {"session_id": "stub", "device_addr": "0x50"}),
        ("i2c_write", {"session_id": "stub", "device_addr": "0x50",
                       "register_addr": "3", "data_hex": "A5"}),
        ("i2c_dump", {"session_id": "stub", "device_addr": "0x50"}),
        ("open_1wire", {"engagement_name": "battery"}),
        ("onewire_search", {"session_id": "stub"}),
        ("onewire_read", {"session_id": "stub", "write_hex": "33", "read_bytes": 8}),
    ]
    for name, args in stubs:
        d = await mcp_call(name, args)
        err = str(d.get("error", ""))
        # Stubs raise "bitpirate-mcp Phase N: <proto> session" or "'stub'";
        # i2c_write hits the safety gate first (confirmation_required).
        stubbed = ("error" in d) or (d.get("confirmation_required") is True)
        bat.record(f"stub-{name}", T, stubbed,
                   f"stubbed/gated as expected ({err[:60]})" if stubbed else
                   f"UNEXPECTED result: {json.dumps(d)[:120]}", json.dumps(d)[:300])


def main() -> int:
    bat = Battery()
    blog = BLog(B_PORT)
    con = Console(A_PORT)
    time.sleep(0.5)

    run_console_tier(bat, con, blog)
    con.close()

    hw = BitPirateHardware.connect(A_PORT)
    server._hardware = hw
    server._hardware_port = A_PORT
    asyncio.run(run_mcp_tier(bat, blog))
    blog.close()

    s = bat.summary()
    print("=" * 60)
    print(f"BATTERY SUMMARY: {s['passed']}/{s['total']} steps passed"
          + (f" (+{s['known']} known limitation)" if s["known"] else ""))
    for tier, t in s["by_tier"].items():
        known = f" (+{t['known']} known)" if t.get("known") else ""
        print(f"  {tier}: {t['pass']}/{t['total']}{known}")
    with open(RESULT_PATH, "w") as f:
        json.dump({"summary": s, "steps": bat.steps,
                   "b_console": blog.snapshot()[-20000:]}, f, indent=2)
    print(f"result -> {RESULT_PATH}")
    return 0 if s["passed"] == s["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
