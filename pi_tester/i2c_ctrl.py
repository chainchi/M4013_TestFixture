"""Two I2C master buses through /dev/i2c-N: scan, raw write/read with repeated start."""

import errno
import subprocess
import threading

from smbus2 import SMBus, i2c_msg

from . import config

BUS_COUNT = len(config.I2C_BUSES)

# Linux reports a missing device as one of these; anything else is a bus fault
_NACK_ERRNOS = {errno.ENXIO, errno.EREMOTEIO, errno.EIO}

_locks = [threading.Lock() for _ in range(BUS_COUNT)]


class Nack(Exception):
    pass


def _check_bus(bus):
    if not 0 <= bus < BUS_COUNT:
        raise ValueError("bus must be 0-1")


def bus_speed(bus):
    """The adapter's clock from the device tree. Linux fixes it at boot, so the
    API's per-transfer "speed" cannot change it (set baudrate= in config.txt)."""
    path = f"/sys/bus/i2c/devices/i2c-{config.I2C_BUSES[bus]['dev']}/device/of_node/clock-frequency"
    try:
        with open(path, "rb") as f:
            return int.from_bytes(f.read(4), "big")
    except OSError:
        return config.I2C_DEFAULT_HZ


def _rdwr(bus, addr, w, rlen):
    msgs = []
    if w:
        msgs.append(i2c_msg.write(addr, w))
    if rlen:
        msgs.append(i2c_msg.read(addr, rlen))
    with SMBus(config.I2C_BUSES[bus]["dev"]) as b:
        try:
            b.i2c_rdwr(*msgs)
        except OSError as e:
            if e.errno in _NACK_ERRNOS:
                raise Nack() from None
            raise
    return bytes(msgs[-1]) if rlen else b""


def scan(bus):
    """Probes 0x08..0x77 with a 1-byte read (the address-only quick write
    confuses some clock-stretching PMBus targets). Returns the list of
    addresses, or raises OSError on a stuck bus."""
    _check_bus(bus)
    found = []
    with _locks[bus]:
        for a in range(0x08, 0x78):
            acks = 0
            try:
                while acks < config.I2C_PROBE_CONFIRM:
                    _rdwr(bus, a, b"", 1)
                    acks += 1
            except Nack:
                pass
            if acks == config.I2C_PROBE_CONFIRM:
                found.append(a)
            elif acks:
                print(f"i2c: bus {bus} 0x{a:02x} answered {acks}/{config.I2C_PROBE_CONFIRM} probes, "
                      "ignored (noise or second master?)", flush=True)
    return found


def line_levels(bus):
    """Idle SDA/SCL levels (1 = pulled high, as an idle bus should be), read
    with pinctrl, which does not take the pins away from the I2C block."""
    _check_bus(bus)
    b = config.I2C_BUSES[bus]
    levels = []
    for pin in (b["sda"], b["scl"]):
        try:
            out = subprocess.run(["pinctrl", "get", str(pin)], capture_output=True, text=True, timeout=2).stdout
            levels.append(1 if "| hi" in out else 0 if "| lo" in out else -1)
        except (OSError, subprocess.SubprocessError):
            levels.append(-1)
    return tuple(levels)


def xfer(bus, addr, w, rlen):
    """Writes w, then (repeated start) reads rlen bytes. Raises Nack."""
    _check_bus(bus)
    if not 0 <= addr <= 0x7f or (not w and not rlen):
        raise ValueError("bad address or empty transfer")
    with _locks[bus]:
        return _rdwr(bus, addr, bytes(w), rlen)
