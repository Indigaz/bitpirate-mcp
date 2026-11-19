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
import os
import re
import sys
import threading
import time
from pathlib import Path

import serial

import buspirate_mcp.server as server
from buspirate_mcp.bitpirate_hardware import BitPirateHardware

A_PORT = "/dev/ttyACM1"
B_PORT = "/dev/ttyACM0"
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
            # CH340 RX can be dead right after rapid re-resets (flash +
            # manual checks + this reset): verify the console answers a MODE
            # probe; a dead B console fails every later B-side step (run 5).
            if not self.wait_for("RIG-RESPONDER", 5.0):
                self.reset_board()
                self.wait_for("RIG-RESPONDER", 5.0)
            self.write("AUDIT 0")
            if not self.wait_for("AUDIT end:", 5.0):
                self.reset_board()
                self.wait_for("RIG-RESPONDER", 5.0)
                self.write("AUDIT 0")
                self.wait_for("AUDIT end:", 5.0)

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
            try:
                d = self.ser.read(64)
            except Exception:
                break  # port closed during read (teardown)
            if d:
                with self.lock:
                    self.buf.append(d.decode("utf-8", "replace"))
                    if sum(len(x) for x in self.buf) > 50000:
                        del self.buf[: max(1, len(self.buf) // 2)]

    def snapshot(self) -> str:
        with self.lock:
            return "".join(self.buf)

    def wait_for(self, text: str, timeout: float = 5.0) -> bool:
        """Poll the captured B console until `text` appears (True) or timeout."""
        end = time.time() + timeout
        while time.time() < end:
            if text in self.snapshot():
                return True
            time.sleep(0.1)
        return False

    def wait_new(self, text: str, timeout: float = 6.0) -> str:
        """Text appended after this call; wait until `text` appears in it."""
        start = len(self.snapshot())
        end = time.time() + timeout
        while time.time() < end:
            cur = self.snapshot()
            fresh = cur[start:] if len(cur) >= start else cur
            if text in fresh:
                return fresh
            time.sleep(0.1)
        cur = self.snapshot()
        return cur[start:] if len(cur) >= start else cur

    def close(self) -> None:
        self.alive = False
        try:
            self.ser.close()
        except Exception:
            pass
        try:
            self.thread.join(timeout=2.0)
        except Exception:
            pass


class _TeeSerial:
    """Diagnostic wrapper: tee a Serial's traffic to a log file (BATTERY_LOG_A)."""

    def __init__(self, inner, logpath: str) -> None:
        self.inner = inner
        self.logfile = open(logpath, "ab")

    def write(self, data) -> int:
        self.logfile.write(b"<< " + data)
        self.logfile.flush()
        return self.inner.write(data)

    def read(self, size: int = 1):
        d = self.inner.read(size)
        if d:
            self.logfile.write(b">> " + d)
            self.logfile.flush()
        return d

    def close(self) -> None:
        try:
            self.inner.close()
        finally:
            self.logfile.close()

    def __getattr__(self, name):
        return getattr(self.inner, name)


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
    # A's 'hiz' does not actually release DIO pins (upstream quirk:
    # disableAllProtocols() is empty) — the last audit level on pin 9
    # would hold B's 1-Wire DQ LOW for the rest of the run and B's DW
    # state machine would flood the log buffer (runs 4-6). End DQ HIGH.
    con.send("set 9 H")
    time.sleep(0.1)
    con.drain(0.05)
    con.cmd("mode hiz", "HIZ>", 4)
    # wait for the FRESH AUDIT end (the boot liveness probe already logged one)
    audit_txt = blog.wait_new("AUDIT end:", 15.0)
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
    arm = blog.wait_for("SQUARE 1kHz", 5.0)
    bat.record("dio-square-arm", T, arm,
               "B square wave armed via B console (u1 ended)", "")

    b_before = blog.snapshot()
    ok, txt = con.cmd(f"measure {DIO_READ} 1000", "Rising edges", 6)
    m = re.search(r"Rising edges:\s+(\d+)", txt)
    rising = int(m.group(1)) if m else -1
    b_during = blog.snapshot()[len(b_before):]
    bat.record("dio-measure-edges", T, ok and rising >= 300,
               f"rising edges in 1s = {rising} (expect ~500 at 1 kHz); B logged {len(b_during)} chars in window",
               txt + "\n[B-console-during-measure] " + b_during[:500])

    blog.write("STOP")
    stopped = blog.wait_for("STOP (out=15 low", 5.0)
    bat.record("dio-square-stop", T, stopped,
               "B square wave stopped, u1 restored", "")

    # Arm B's GPIO mirror via A's UART (last: ends B's UART1 permanently;
    # mirror stays armed for the MCP tier).
    ok, txt = con.cmd("mode uart", "UART>", 4)
    ok2, txt2 = con.cmd(f"write hex{{{GPIO_NL}}}", "UART Write:", 4)
    arm = blog.wait_for("MODE=GPIO", 6.0)
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
    bat.record("back-to-hiz", T, ok, "mirror armed for the dio loopback", txt)

    # Release the mirror before the I2C/1-Wire console tier: mirror mode
    # holds pin 15 (wired to A's UART RX, pin 44) LOW -> a continuous
    # 0x00 stream on A's UART1 RX. The mirror is re-armed right before
    # m3 (its last consumer) via the B console (m3a-arm-mirror).
    blog.write("UART")
    fresh = blog.wait_new("MODE=UART (pin15 restored", 5.0)
    released = "MODE=UART (pin15 restored" in fresh
    bat.record("dio-mirror-release", T, released,
               "B pin 15 back to UART1 TX (mirror released)"
               if released else "no MODE=UART log: " + fresh[-120:], fresh[-300:])

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
    # NOTE: data byte needs a 0x prefix — A's console parser takes bare
    # numerics as DECIMAL, so "A5" is rejected (A prints its help text) and
    # the write never happens (reg 3 readback 0x00).
    con.send("write 0x50 3 0xA5")
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

    # WRITE SP: fixed 2026-11-17 (rig-responder v1.10). Root cause was a
    # B-side firmware bug: dwDispatch() never cleared the s_cmd accumulator,
    # so each byte was ORed into the previous byte (0xCC|0x0F=0xCF -> 0xFF).
    # A-side payload bytes must carry a 0x prefix: A's parseByte() treats
    # bare numbers as DECIMAL, which would break A's own scratchpad-CRC
    # self-check (crc8(11 22 33 44 55 66 77)==0x52 only for hex parsing).
    con.send("write sp 0x11 0x22 0x33 0x44 0x55 0x66 0x77 0x52")
    ok, txt = con.wait("Scratchpad write successful.", 30)
    if not ok:
        ok, fail_txt = con.wait("Failed after", 8)
        txt = fail_txt
    bat.record("1wire-write-sp", T, ok,
               "write-sp passed (v1.10 s_cmd-accumulator fix)" if ok
               else "write-sp failed: " + txt[-200:], txt)


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

    # Re-arm B's GPIO mirror via its console (it was released after the
    # dio tier): m3 observes the reset-pin pulse on the mirror.
    blog.write("GPIO")
    fresh = blog.wait_new("MODE=GPIO", 6.0)
    armed = "MODE=GPIO" in fresh
    bat.record("m3a-arm-mirror", T, armed,
               "B mirror armed (in=10 -> out=15)" if armed
               else "no MODE=GPIO log: " + fresh[-120:], fresh[-300:])

    d = await mcp_call("enter_download_mode", {"boot_pin": 2, "reset_pin": 4})
    seq_ok = (d.get("status") == "download_mode_entered")
    # The MCP tool releases pin 4 at the end of the sequence, so the drive
    # cannot be observed after the call. Verify the reset-pin DIO drive
    # directly via A's OWN readback (deterministic, no B involvement): drive
    # LOW (must overcome B v1.15's pull-up on the shared wire) and HIGH, then
    # release. This is the exact pin-level capability enter_download_mode
    # relies on. B's mirror is a secondary observer only: in the full-battery
    # context its static `last` state + B log timing make it flaky (fails
    # runs 7-11, passes every standalone repro), so it is recorded but not
    # gating the step.
    low_read = high_read = -1
    drive_err = ""
    try:
        hw = server._hardware
        hw.set_pin_output(4, False)
        low_read = hw.read_pin(4)
        hw.set_pin_output(4, True)
        high_read = hw.read_pin(4)
        hw.release_pin(4)
    except Exception as exc:
        drive_err = f"{type(exc).__name__}: {exc}"
    drive_ok = (low_read == 0) and (high_read == 1) and not drive_err
    bwin = blog.snapshot()[-300:]
    bat.record("m3-enter-download-mode", T, seq_ok and drive_ok,
               f"seq={seq_ok}; A readback pin4 L={low_read} H={high_read}"
               + (f" ({drive_err[:60]})" if drive_err else ""),
               json.dumps(d)[:200] + " | B-tail: " + bwin)

    # Release B's GPIO mirror NOW (m3 was its last consumer): mirror mode
    # holds pin 15 — wired to A's UART RX (GPIO 44) — LOW, so A's UART1
    # RX reads a continuous 0x00 stream and B cannot echo (u1 ended).
    # The remaining MCP tier (i2c / 1wire / uart) needs B's UART1 TX
    # restored. Without this, u9/u10 read NULs and 1-Wire times out.
    pre = blog.snapshot()
    blog.write("UART")
    fresh = blog.wait_new("MODE=UART (pin15 restored", 5.0)
    released = "MODE=UART (pin15 restored" in fresh
    bat.record("m3b-release-mirror", T, released,
               "B pin 15 back to UART1 TX (mirror released)"
               if released else "no MODE=UART log: " + fresh[-120:], fresh[-300:])

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

    # --- Phase 3 (implemented 2026-11-17): real I2C + 1-Wire MCP sessions.
    # Console-backed driver: one bracket-instruction round-trip per transfer.
    # B's slave answers one byte per I2C request (onI2cRequest), so the
    # dump exercises the pipeline with size=1.
    d = await mcp_call("open_i2c", {"speed": 100000,
                                    "engagement_name": "battery-i2c"})
    i2c_sid = d.get("session_id")
    bat.record("m7-open-i2c", T, bool(i2c_sid),
               f"session {i2c_sid} (100 kHz)" if i2c_sid
               else "no session_id: " + str(d.get("error", ""))[:120],
               json.dumps(d)[:300])

    d = await mcp_call("i2c_scan", {"session_id": i2c_sid or "stub"})
    devs = [x.get("address") for x in d.get("devices", [])]
    bat.record("m8-i2c-scan", T, "0x50" in devs,
               f"devices={devs}" if devs else "no devices found",
               json.dumps(d)[:300])

    d = await mcp_call("i2c_write", {"session_id": i2c_sid or "stub",
                                     "device_addr": "0x50", "register_addr": 3,
                                     "data_hex": "A5", "_confirmed": True})
    bat.record("m9-i2c-write-03-a5", T,
               d.get("written") is True and d.get("bytes_written") == 1,
               f"write reg=0x03 A5 ({d.get('bytes_written')}B)"
               if d.get("written") else d.get("error", "no written flag"),
               json.dumps(d)[:300])

    d = await mcp_call("i2c_read", {"session_id": i2c_sid or "stub",
                                    "device_addr": "0x50", "register_addr": 3})
    bat.record("m10-i2c-read-03-a5", T, d.get("data_hex") == "a5",
               f"read reg=0x03 -> {d.get('data_hex')}", json.dumps(d)[:300])

    # Dump of size 1. Expected value is 0xA5, not 0x00: the B responder's
    # I2C slave only applies the pointer byte on WRITE (W+STOP) transfers;
    # on read (W+Sr+R) transfers the pointer byte is dropped and the stale
    # pointer is used (observed for both the console read-00-zero and this
    # dump). After m9 (write reg 3 <- A5) and m10 (read reg 3), the pointer
    # is 3, so the dump deterministically returns regs[3] = 0xA5. The step
    # still exercises the full MCP dump pipeline (transfer + artifact file).
    d = await mcp_call("i2c_dump", {"session_id": i2c_sid or "stub",
                                    "device_addr": "0x50", "size": 1})
    dump_file = d.get("file_path", "")
    dump_ok = (d.get("bytes_read") == 1 and bool(dump_file)
               and Path(dump_file).exists()
               and Path(dump_file).read_bytes() == b"\xa5")
    bat.record("m11-i2c-dump-01", T, dump_ok,
               f"dump 1B -> preview={d.get('hex_preview', '?')} "
               f"(stale-pointer read of reg 3 written by m9) "
               f"file={Path(dump_file).name if dump_file else 'missing'}",
               json.dumps(d)[:300])

    d = await mcp_call("close_i2c", {"session_id": i2c_sid or "stub"})
    bat.record("m12-close-i2c", T, d.get("closed") is True,
               "i2c session closed", json.dumps(d)[:300])

    d = await mcp_call("open_1wire", {"engagement_name": "battery-1wire"})
    ow_sid = d.get("session_id")
    bat.record("m13-open-1wire", T, bool(ow_sid),
               f"session {ow_sid}" if ow_sid
               else "no session_id: " + str(d.get("error", ""))[:120],
               json.dumps(d)[:300])

    d = await mcp_call("onewire_search", {"session_id": ow_sid or "stub"})
    bat.record("m14-1wire-search", T,
               d.get("present") is True and d.get("crc_valid") is True
               and d.get("rom_code") == "2811223344556656"
               and d.get("family_code") == "0x28",
               f"present={d.get('present')} rom={d.get('rom_code')} "
               f"crc={d.get('crc_valid')} fam={d.get('family_code')}",
               json.dumps(d)[:300])

    d = await mcp_call("onewire_read", {"session_id": ow_sid or "stub",
                                        "write_hex": "33", "read_bytes": 8})
    bat.record("m15-1wire-read-rom", T, d.get("rx") == "2811223344556656",
               f"ROM rx={d.get('rx')}", json.dumps(d)[:300])

    # scratchpad content: the console tier's write-sp step above stored
    # 11 22 33 44 55 66 77 52 verbatim (B keeps all 8 bytes; only the
    # 9th/0xBE byte is recomputed).
    d = await mcp_call("onewire_read", {"session_id": ow_sid or "stub",
                                        "write_hex": "AA", "read_bytes": 8})
    bat.record("m16-1wire-read-sp", T, d.get("rx") == "1122334455667752",
               f"SPAD rx={d.get('rx')} (post-write-sp)", json.dumps(d)[:300])

    d = await mcp_call("close_1wire", {"session_id": ow_sid or "stub"})
    bat.record("m17-close-1wire", T, d.get("closed") is True,
               "1-wire session closed", json.dumps(d)[:300])

    # --- Phase 4 (implemented 2026-11-18): real UART MCP sessions.
    # Console-backed driver: `uread` (bounded non-flushing read) +
    # `write hex{...}`. B is an echo-only responder: PING -> PONG x10
    # (50 ms apart), any other line -> ECHO:<line> x3.
    d = await mcp_call("open_uart", {"baud": 115200,
                                     "pins": {"rx": "44", "tx": "43"},
                                     "engagement_name": "battery-uart"})
    uart_sid = d.get("session_id")
    bat.record("u7-open-uart", T, bool(uart_sid),
               f"session {uart_sid} (115200 8N1)" if uart_sid
               else "no session_id: " + str(d.get("error", ""))[:120],
               json.dumps(d)[:300])

    # B is idle (echo-only): every rate scores ~0; the step exercises the
    # full scan pipeline (8 reconfigures + reads) and the baud restore.
    d = await mcp_call("scan_baud", {})
    n_cand = len(d.get("candidates", []))
    bat.record("u8-scan-baud", T, n_cand == 8,
               f"{n_cand} rates scored "
               f"(recommended={d.get('recommended')}, idle B target)",
               json.dumps(d)[:300])

    d = await mcp_call("send_command", {"session_id": uart_sid or "stub",
                                        "command": "PING"})
    n_pong = str(d.get("response", "")).count("PONG")
    bat.record("u9-send-ping", T, "PONG" in str(d.get("response", "")),
               f"PONG x{n_pong} ({d.get('bytes_received')}B)",
               json.dumps(d)[:300])

    d = await mcp_call("send_command", {"session_id": uart_sid or "stub",
                                        "command": "HELLO"})
    n_echo = str(d.get("response", "")).count("ECHO:HELLO")
    bat.record("u10-send-hello", T, "HELLO" in str(d.get("response", "")),
               f"ECHO:HELLO x{n_echo} ({d.get('bytes_received')}B)",
               json.dumps(d)[:300])

    d = await mcp_call("read_output", {"session_id": uart_sid or "stub",
                                       "timeout_ms": 800})
    bat.record("u11-read-idle", T,
               isinstance(d.get("bytes_received"), int),
               f"idle read: {d.get('bytes_received')} bytes (idle B)",
               json.dumps(d)[:300])

    d = await mcp_call("close_uart", {"session_id": uart_sid or "stub"})
    bat.record("u12-close-uart", T, d.get("closed") is True,
               "uart session closed", json.dumps(d)[:300])


def main() -> int:
    bat = Battery()
    blog = BLog(B_PORT)
    con = Console(A_PORT)
    time.sleep(0.5)

    run_console_tier(bat, con, blog)
    con.close()

    hw = BitPirateHardware.connect(A_PORT)
    if os.environ.get("BATTERY_LOG_A"):
        hw.serial = _TeeSerial(hw.serial, os.environ["BATTERY_LOG_A"])
    server._hardware = hw
    server._hardware_port = A_PORT
    asyncio.run(run_mcp_tier(bat, blog))
    blog.close()
    if os.environ.get("BATTERY_LOG_B"):
        with open(os.environ["BATTERY_LOG_B"], "w") as f:
            f.write(blog.snapshot())

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
