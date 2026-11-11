"""Live smoke test: BitPirateHardware against /dev/ttyACM0 (read-only DIO)."""
import sys

sys.path.insert(0, "src")
from buspirate_mcp.bitpirate_hardware import BitPirateHardware

print("devices:", BitPirateHardware.list_devices())
print("terminal port:", BitPirateHardware.find_terminal_port())

h = BitPirateHardware.connect("/dev/ttyACM0")
print("connected, active_mode:", h._active_mode)

v = h.read_pin(42)
print("GPIO42 read:", v)
assert v in (0, 1)

m = h.measure_pin(42, 1500)
print("GPIO42 measure (1.5s):", m)

h.set_pin_output(42, True)
v2 = h.read_pin(42)
print("GPIO42 after drive HIGH:", v2)
assert v2 == 1, "drive-high self-test failed"

h.set_pin_output(42, False)
v3 = h.read_pin(42)
print("GPIO42 after drive LOW:", v3)
assert v3 == 0, "drive-low self-test failed"

h.release_pin(42)
print("released; active_mode:", h._active_mode)
h.disconnect()
print("SMOKE OK")
