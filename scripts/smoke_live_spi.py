"""Live smoke test Phase 2: SPI flash shell against /dev/ttyACM0.

No flash chip is attached -- exercises the 'no chip' paths, shell
lifecycle, and instruction syntax. Read-only.
"""
import sys

sys.path.insert(0, "src")
from buspirate_mcp.bitpirate_hardware import BitPirateHardware

h = BitPirateHardware.connect("/dev/ttyACM0")
print("connected")

# instruction syntax first (no shell needed)
rx = h.spi_instruction("[0x9F r:3]", timeout=20)
print("instruction [0x9F r:3] ->", rx.hex() if rx else "(empty)")

# probe via flash shell
r = h.spi_flash_probe(timeout=25)
print("probe:", r)

# analyze (should short-circuit: no chip)
t = h.spi_flash_analyze(timeout=30)
print("analyze tail:", t.strip().splitlines()[-1] if t.strip() else "(empty)")

# exit shell
h._flash_exit(timeout=10)
print("shell closed:", h._flash_shell_open)

h.disconnect()
print("SMOKE2 OK")
