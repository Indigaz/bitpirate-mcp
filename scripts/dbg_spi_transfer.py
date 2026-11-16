#!/usr/bin/env python3
"""Debug: capture board B's console while driving one SPI transfer via the MCP."""
import asyncio, json, threading, time
import serial
import buspirate_mcp.server as server
from buspirate_mcp.bitpirate_hardware import BitPirateHardware

A_PORT = "/dev/ttyACM0"
B_PORT = "/dev/ttyACM1"
SPI_PINS = {"mosi": 9, "miso": 8, "sclk": 7, "cs": 4}

b_log = []
b_stop = False

def read_b():
    s = serial.Serial(B_PORT, 115200, timeout=0.1)
    while not b_stop:
        c = s.read(4096)
        if c:
            b_log.append(c.decode("utf-8", "replace"))
    s.close()

async def call(name, args):
    contents = await server.call_tool(name, args)
    return json.loads(contents[0].text)

async def main():
    t = threading.Thread(target=read_b, daemon=True)
    t.start()
    time.sleep(0.5)

    hw = BitPirateHardware.connect(A_PORT)
    server._hardware = hw
    server._hardware_port = A_PORT

    d = await call("open_spi", {"engagement_name": "dbg"})
    sess = d.get("session_id")
    await call("spi_config", {**SPI_PINS, "freq_mhz": 2})
    print("=== driving [0x9F D:2 r:3] ===")
    d = await call("spi_transfer", {"instruction": "[0x9F D:2 r:3]"})
    print("MCP result:", json.dumps(d))
    print("=== driving [0x9F D:5 r:3] (longer delay) ===")
    d = await call("spi_transfer", {"instruction": "[0x9F D:5 r:3]"})
    print("MCP result:", json.dumps(d))
    await call("close_spi", {"session_id": sess})

    time.sleep(0.5)
    b_stop = True
    t.join(timeout=1)
    print("\n=== BOARD B CONSOLE ===")
    print("".join(b_log))

asyncio.run(main())
