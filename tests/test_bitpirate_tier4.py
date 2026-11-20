"""Tier 4 driver tests: I2C bidirectional (ping + slave session) + servo.

Transcript-driven against the v1.8 firmware source:
- I2cController.cpp handlePing ("Ping 0xNN: I2C Ping: ACK received! ...")
- I2cController.cpp handleSlave (interactive loop: "Listening" / "Stopped by user")
- I2cService.cpp onSlaveReceive/onSlaveRequest ("Master wrote: 0x ..." /
  "Master requested read")
- DioController.cpp handleServo + PinService::setServoAngle (50 Hz, 1-2 ms)
"""

from __future__ import annotations

import pytest

from buspirate_mcp.bitpirate_hardware import BitPirateCLIError, BitPirateHardware
from test_bitpirate_hardware import FakeSerial

PROMPT_I2C = "I2C> "
PROMPT_DIO = "DIO> "


@pytest.fixture
def hw() -> tuple[BitPirateHardware, FakeSerial]:
    fake = FakeSerial()
    h = BitPirateHardware.__new__(BitPirateHardware)
    h.port = "/dev/ttyACM1"
    h.baud = 115200
    h.serial = fake  # type: ignore[assignment]
    h._active_mode = None
    h._flash_shell_open = False
    h._slave_active = False
    h._slave_addr = 0
    h.i2c_sda = 5
    h.i2c_scl = 6
    h.i2c_speed = 100000
    h.onewire_pin = 9
    return h, fake


def _i2c_walk(fake: FakeSerial) -> None:
    """I2C mode-entry config walk (SDA/SCL/Frequency prompts)."""
    fake.expect("mode i2c", "I2C Configuration:\nSDA GPIO [5]: ")
    fake.expect("5", "SCL GPIO [6]: ")
    fake.expect("6", "Frequency [400000]: ")
    fake.expect("100000", "I2C configured.\r\n" + PROMPT_I2C)


def test_i2c_ping_parses_ack(hw):
    h, fake = hw
    _i2c_walk(fake)
    fake.expect("ping 0x50", "Ping 0x50: I2C Ping: ACK received! Device is present.\r\n" + PROMPT_I2C)
    r = h.i2c_ping(0x50)
    assert r == {"addr": 0x50, "ack": True}


def test_i2c_ping_parses_nack(hw):
    h, fake = hw
    h._active_mode = "i2c"
    fake.expect("ping 0x21", "Ping 0x21: I2C Ping: No response (NACK or error).\r\n" + PROMPT_I2C)
    r = h.i2c_ping(0x21)
    assert r == {"addr": 0x21, "ack": False}


def test_i2c_ping_validates_addr(hw):
    h, _ = hw
    with pytest.raises(ValueError, match="0x08-0x77"):
        h.i2c_ping(0x07)
    with pytest.raises(ValueError, match="0x08-0x77"):
        h.i2c_ping(0x78)


def test_open_i2c_slave_waits_listening(hw):
    h, fake = hw
    h._active_mode = "i2c"
    fake.expect("slave 0x60", "I2C Slave: Listening on address 0x60... Press [ENTER] to stop.\n")
    r = h.open_i2c_slave(0x60)
    assert r == {"addr": 0x60}
    assert h._slave_active is True
    assert h._slave_addr == 0x60


def test_open_i2c_slave_rejects_double(hw):
    h, _ = hw
    h._slave_active = True
    with pytest.raises(BitPirateCLIError, match="already active"):
        h.open_i2c_slave(0x61)


def test_i2c_slave_log_reports_entries(hw):
    h, fake = hw
    h._slave_active = True
    h._slave_addr = 0x60
    fake.queue_rx("Master wrote: 0x 01 A5 B6\nMaster requested read\n")
    r = h.i2c_slave_log()
    entries = r["entries"]
    assert any("Master wrote: 0x 01 A5 B6" in e for e in entries)
    assert any("Master requested read" in e for e in entries)


def test_i2c_slave_log_requires_session(hw):
    h, _ = hw
    with pytest.raises(BitPirateCLIError, match="No I2C slave session"):
        h.i2c_slave_log()


def test_close_i2c_slave_returns_buffered_log(hw):
    h, fake = hw
    h._slave_active = True
    h._slave_addr = 0x60
    fake.queue_rx(
        "Master wrote: 0x 01 A5 B6\n"
        "Master requested read\n"
        "\r\nI2C Slave: Stopped by user.\r\n" + PROMPT_I2C
    )
    r = h.close_i2c_slave()
    assert r["addr"] == 0x60
    entries = r["entries"]
    assert any("Master wrote: 0x 01 A5 B6" in e for e in entries)
    assert any("Master requested read" in e for e in entries)
    assert h._slave_active is False
    assert h._slave_addr == 0


def test_close_i2c_slave_requires_stop(hw):
    h, fake = hw
    h._slave_active = True
    h._slave_addr = 0x60
    fake.queue_rx("\r\n" + PROMPT_I2C)  # no "Stopped by user" line
    with pytest.raises(BitPirateCLIError, match="did not stop"):
        h.close_i2c_slave()


def test_close_i2c_slave_requires_session(hw):
    h, _ = hw
    with pytest.raises(BitPirateCLIError, match="No I2C slave session"):
        h.close_i2c_slave()


def test_servo_parses_angle(hw):
    h, fake = hw
    h._active_mode = "dio"
    fake.expect("servo 4 90", "DIO Servo: Set GPIO 4 to angle 90... Use 'reset 4' to stop.\r\n" + PROMPT_DIO)
    r = h.servo(4, 90)
    assert r == {"pin": 4, "angle": 90}
    assert "servo 4 90" in fake.lines_sent


def test_servo_validates_angle(hw):
    h, _ = hw
    with pytest.raises(ValueError, match="0-180"):
        h.servo(4, 181)
    with pytest.raises(ValueError, match="0-180"):
        h.servo(4, -1)
