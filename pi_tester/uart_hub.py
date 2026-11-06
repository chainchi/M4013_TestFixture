"""The 4 DUT UARTs. A CP2108 shows up as 4 ttyUSB devices through the kernel's
cp210x driver; FTDI, CH34x, CDC-ACM and the Pi's own UARTs work the same way.
Each port keeps a 256 KB scrollback ring; readers follow it with a cursor."""

import glob
import os
import re
import threading
import time

import serial

from . import config


class _Port:
    def __init__(self):
        self.lock = threading.Lock()   # guards ser and the ring
        self.ser = None
        self.dev = ""
        self.baud = config.UART_DEFAULT_BAUD
        self.ring = bytearray(config.UART_RING_SIZE)
        self.total = 0                 # bytes ever received
        self.tx = 0                    # bytes ever sent
        self.base = 0                  # bytes dropped by clear()


_ports = [_Port() for _ in range(config.UART_PORTS)]
_last_usb_id = ""


def _usb_id(dev):
    """VID:PID of the USB device behind a tty, "" for a non-USB UART."""
    path = os.path.realpath(f"/sys/class/tty/{os.path.basename(os.path.realpath(dev))}/device")
    while path != "/":
        try:
            with open(f"{path}/idVendor") as v, open(f"{path}/idProduct") as p:
                return f"{v.read().strip().upper()}:{p.read().strip().upper()}"
        except OSError:
            path = os.path.dirname(path)
    return ""


def _discover():
    """Device path for each port, in order."""
    if config.UART_DEVICES:
        return list(config.UART_DEVICES)

    def key(link):
        m = re.search(r"-if(\d+)", link)
        return (re.sub(r"-if\d+.*", "", link), int(m.group(1)) if m else 0)

    devs = sorted(glob.glob("/dev/serial/by-id/*"), key=key)
    # Clone adapters can share a serial number, so two of them get one by-id
    # link; add any tty that link misses under its by-path name (per USB socket).
    seen = {os.path.realpath(d) for d in devs}
    for d in sorted(glob.glob("/dev/serial/by-path/*")):
        if os.path.realpath(d) not in seen:
            seen.add(os.path.realpath(d))
            devs.append(d)
    return devs


def _ring_put(p, data):
    with p.lock:
        n, size = len(data), len(p.ring)
        start = p.total % size
        first = min(n, size - start)
        p.ring[start:start + first] = data[:first]
        p.ring[:n - first] = data[first:]
        p.total += n


def _reader(idx):
    p = _ports[idx]
    while True:
        ser = p.ser
        if ser is None:
            return
        try:
            data = ser.read(ser.in_waiting or 1)
        except (OSError, serial.SerialException):
            print(f"uart: port {idx} disconnected", flush=True)
            with p.lock:
                p.ser = None
            try:
                ser.close()
            except Exception:
                pass
            return
        if data:
            _ring_put(p, data)


def _open(idx, dev):
    global _last_usb_id
    p = _ports[idx]
    try:
        ser = serial.Serial(dev, p.baud, timeout=0.1, write_timeout=0.5)
    except (OSError, serial.SerialException):
        return
    try:
        # assert DTR/RTS: some adapters gate their RS232 drivers on them
        ser.dtr = ser.rts = True
    except (OSError, serial.SerialException):
        pass                           # UART without modem lines
    _last_usb_id = _usb_id(dev) or _last_usb_id
    with p.lock:
        p.ser, p.dev = ser, dev
    threading.Thread(target=_reader, args=(idx,), daemon=True, name=f"uart{idx}").start()
    print(f"uart: port {idx} open on {dev}, {p.baud} baud", flush=True)


def _connect_loop():
    """Opens any port that is not open yet; covers the adapter being plugged
    in after boot and replugged mid-test."""
    while True:
        devs = _discover()
        in_use = {p.dev for p in _ports if p.ser is not None}
        for i, dev in enumerate(devs[:config.UART_PORTS]):
            if _ports[i].ser is None and dev not in in_use:
                _open(i, dev)
        time.sleep(config.UART_RESCAN_S)


def init():
    threading.Thread(target=_connect_loop, daemon=True, name="uart_hub").start()


def connected(port):
    return _ports[port].ser is not None


def last_usb_id():
    return _last_usb_id


def baud(port):
    return _ports[port].baud


def device(port):
    return _ports[port].dev if connected(port) else ""


def _check(port):
    if not 0 <= port < config.UART_PORTS:
        raise ValueError("port must be 0-3")


def set_baud(port, b):
    _check(port)
    if not 300 <= b <= 2_000_000:
        raise ValueError("baud must be 300-2000000")
    p = _ports[port]
    with p.lock:
        p.baud = b
        if p.ser:                      # else applied on next open
            p.ser.baudrate = b


def write(port, data):
    _check(port)
    p = _ports[port]
    with p.lock:
        ser = p.ser
    if ser is None:
        raise ConnectionError("port not connected")
    ser.write(data)
    p.tx += len(data)


def clear(port):
    _check(port)
    p = _ports[port]
    with p.lock:
        p.base = p.total


def rx_total(port):
    return _ports[port].total


def tx_total(port):
    return _ports[port].tx


def read_since(port, pos, max_len):
    """Bytes received after pos (at most max_len) and the new cursor. If the
    cursor fell out of the scrollback, it skips forward to the oldest byte kept."""
    p = _ports[port]
    with p.lock:
        size = len(p.ring)
        oldest = max(p.total - size, p.base, 0)
        pos = max(pos, oldest)
        end = min(p.total, pos + max_len)
        if pos >= end:
            return b"", pos
        a, b = pos % size, end % size
        data = bytes(p.ring[a:b]) if a < b else bytes(p.ring[a:]) + bytes(p.ring[:b])
        return data, end
