"""Live system view: /system draws the architecture, /api/system feeds it.

One JSON snapshot of everything the page shows. Slow probes (systemctl,
vcgencmd, the DUT's TCP port) are cached; the I2C devices are read only while
the page asks for them (i2c=1), so an idle tester puts no traffic on the buses."""

import asyncio
import os
import shutil
import socket
import subprocess
import time
from collections import Counter, deque

from aiohttp import web

from . import agent, camera, config, i2c_ctrl, io_ctrl, net, settings, uart_hub

_PAGE = os.path.join(os.path.dirname(__file__), "web", "system.html")
_STARTED = time.time()
SLOW_S = 10                            # systemctl / vcgencmd / DUT probe
I2C_S = 3                              # PSU + EEPROM poll
LM75_S = 1                             # LM75 probe, fast enough to catch a loose wire
EEPROM_ADDR, PSU_ADDR = 0x50, 0x40
LM75_BUS, LM75_ADDRS = 0, range(0x48, 0x50)   # all 8 straps, so a changing address shows

extra = lambda: {}                     # web_server sets this: its client/stream counts
_cache = {}                            # key -> (time, value)
_cpu = [0, 0]                          # last (busy, total) jiffies
_i2c_lock = asyncio.Lock()


def _cached(key, period, fn):
    t, v = _cache.get(key, (0, None))
    if time.monotonic() - t > period:
        v = fn()
        _cache[key] = (time.monotonic(), v)
    return v


def _read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def _run(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""

# ---------- host ----------


def _cpu_pct():
    f = [int(x) for x in _read("/proc/stat", "cpu 0").split("\n")[0].split()[1:]]
    busy, total = sum(f) - sum(f[3:5]), sum(f)       # idle + iowait
    d_busy, d_total = busy - _cpu[0], total - _cpu[1]
    _cpu[:] = busy, total
    return round(100 * d_busy / d_total, 1) if d_total > 0 else 0.0


def _meminfo():
    m = {}
    for line in _read("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        m[k] = int(v.split()[0]) if v.split() else 0
    return m


def _throttled():
    out = _run("vcgencmd", "get_throttled")          # "throttled=0x50000"
    try:
        return int(out.split("=")[1], 16)
    except (IndexError, ValueError):
        return None


def _host():
    mem = _meminfo()
    disk = shutil.disk_usage("/")
    up = float(_read("/proc/uptime", "0").split()[0])
    temp = _read("/sys/class/thermal/thermal_zone0/temp")
    return {
        "name": socket.gethostname(),
        "uptime_s": int(up),
        "service_uptime_s": int(time.time() - _STARTED),
        "cpu_pct": _cpu_pct(),
        "cores": os.cpu_count(),
        "load": [round(x, 2) for x in os.getloadavg()],
        "temp_c": round(int(temp) / 1000, 1) if temp.lstrip("-").isdigit() else None,
        "mem_pct": round(100 * (1 - mem.get("MemAvailable", 0) / max(1, mem.get("MemTotal", 1))), 1),
        "mem_total_mb": mem.get("MemTotal", 0) // 1024,
        "disk_pct": round(100 * disk.used / disk.total, 1),
        "throttled": _cached("throttled", SLOW_S, _throttled),
    }


def _services():
    def check():
        return {s: _run("systemctl", "is-active", s) or "unknown" for s in ("cloudflared", "ssh")}
    return _cached("services", SLOW_S, check)

# ---------- network ----------


def _if_bytes(ifname):
    d = f"/sys/class/net/{ifname}/statistics/"
    return int(_read(d + "rx_bytes", "0") or 0), int(_read(d + "tx_bytes", "0") or 0)


def _dut_probe():
    s = settings.get()
    host = s["dut_host"].strip()
    if not host:
        return {"host": "", "reachable": None}
    port = 443 if s.get("dut_https", True) else 80
    t0 = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=0.8):
            return {"host": host, "port": port, "reachable": True, "ms": round(1000 * (time.monotonic() - t0))}
    except OSError:
        return {"host": host, "port": port, "reachable": False}


def _net():
    wifi_ok, rssi = net.wifi_status()
    s = settings.get()
    return {
        "eth": {"link": net.link_up(), "ip": net.ipv4(config.ETH_IF), "bytes": _if_bytes(config.ETH_IF)},
        "wifi": {"connected": wifi_ok, "ssid": s["wifi_ssid"], "rssi": rssi, "ip": net.ipv4(config.WIFI_IF),
                 "bytes": _if_bytes(config.WIFI_IF)},
        "dut": _cached("dut", SLOW_S, _dut_probe),
    }

# ---------- bench ----------


def _linear11(lo, hi):
    w = lo | hi << 8
    n, y = w >> 11, w & 0x7FF
    n, y = n - 32 if n > 15 else n, y - 2048 if y > 1023 else y
    return y * 2.0 ** n


_glitches = {"eeprom": 0, "psu": 0}    # reads that only worked on the retry


def _i2c_devices():
    """Read-only probes of the two known devices; the counters skip these.
    A NACK is retried once, so one missed read shows as a glitch, not as gone."""
    dev = "eeprom"

    def rd(bus, addr, cmd, n):
        w = bytes([cmd]) if cmd is not None else b""
        try:
            return i2c_ctrl.xfer(bus, addr, w, n, count=False)
        except i2c_ctrl.Nack:
            time.sleep(0.01)
            data = i2c_ctrl.xfer(bus, addr, w, n, count=False)
            _glitches[dev] += 1
            return data

    out = {}
    try:
        rd(0, EEPROM_ADDR, None, 1)                  # current-address read: no write to the chip
        out["eeprom"] = {"present": True}
    except (i2c_ctrl.Nack, OSError) as e:
        out["eeprom"] = {"present": False, "error": "no ACK" if isinstance(e, i2c_ctrl.Nack) else str(e)}
    out["eeprom"]["glitches"] = _glitches["eeprom"]
    dev = "psu"
    psu = {"present": False}
    try:
        vin, vout, iout, pout = rd(1, PSU_ADDR, 0x88, 2), rd(1, PSU_ADDR, 0x8B, 2), rd(1, PSU_ADDR, 0x8C, 2), rd(1, PSU_ADDR, 0x96, 2)
        sw, cml = rd(1, PSU_ADDR, 0x79, 2), rd(1, PSU_ADDR, 0x7E, 1)
        psu = {"present": True, "vin": round(_linear11(*vin), 2), "vout": round((vout[0] | vout[1] << 8) / 512, 3),
               "iout": round(_linear11(*iout), 2), "pout": round(_linear11(*pout), 1),
               "status_word": sw[0] | sw[1] << 8, "cml": cml[0]}
    except (i2c_ctrl.Nack, OSError) as e:
        psu["error"] = "no ACK" if isinstance(e, i2c_ctrl.Nack) else str(e)
    psu["glitches"] = _glitches["psu"]
    out["psu"] = psu
    return out


_lm75 = {"polls": 0, "hits": 0, "recent": deque(maxlen=60), "seen": Counter(), "last_seen": None}


def _lm75_temp(raw):
    v = raw[0] << 8 | raw[1]
    return (v - 0x10000 if v & 0x8000 else v) / 256       # 0.5 C steps on the LM75, finer on clones


def _lm75_poll():
    """No retry here on purpose: every miss counts, that's what this is for."""
    found, temp, err = [], None, None
    for a in LM75_ADDRS:
        try:
            raw = i2c_ctrl.xfer(LM75_BUS, a, bytes(1), 2, count=False)   # temperature register
        except i2c_ctrl.Nack:
            continue
        except OSError as e:
            err = str(e)
            continue
        found.append(a)
        if temp is None:
            temp = _lm75_temp(raw)
    s = _lm75
    s["polls"] += 1
    s["recent"].append(1 if found else 0)
    if found:
        s["hits"] += 1
        s["last_seen"] = time.time()
        s["seen"].update(found)
    return {"addrs": found, "temp": temp, "error": err, "polls": s["polls"], "hits": s["hits"],
            "recent": sum(s["recent"]), "recent_of": len(s["recent"]), "last_seen": s["last_seen"],
            "seen": {f"0x{a:02X}": n for a, n in sorted(s["seen"].items())}}


async def _i2c(poll):
    buses = [{"bus": b, "dev": config.I2C_BUSES[b]["dev"], "speed": i2c_ctrl.bus_speed(b),
              "ops": i2c_ctrl.ops[b], "nacks": i2c_ctrl.nacks[b], "errors": i2c_ctrl.errors[b]} for b in range(i2c_ctrl.BUS_COUNT)]
    r = {"buses": buses}
    if poll:
        async with _i2c_lock:                         # several open pages share one poll
            # LM75 first: a burst of PSU reads on bus 1 makes it NACK for up to ~100 ms
            # (power coupling on the bench), and the page shouldn't cause its own misses
            t, lm = _cache.get("lm75", (0, None))
            if time.monotonic() - t > LM75_S * 0.9:
                lm = await asyncio.to_thread(_lm75_poll)
                _cache["lm75"] = (time.monotonic(), lm)
            t, v = _cache.get("i2c", (0, None))
            if time.monotonic() - t > I2C_S:
                v = await asyncio.to_thread(_i2c_devices)
                v["at"] = time.time()
                _cache["i2c"] = (time.monotonic(), v)
        r.update(v, lm75=lm)
    return r


def _bench():
    return {
        "gpio_out": [io_ctrl.gpio_out_get(i) for i in range(io_ctrl.OUT_COUNT)],
        "gpio_in": [io_ctrl.gpio_in_get(i) for i in range(io_ctrl.IN_COUNT)],
        "pwm": [io_ctrl.pwm_get(i) for i in range(io_ctrl.PWM_COUNT)],
        "uart": [{"port": i, "connected": uart_hub.connected(i), "dev": uart_hub.device(i),
                  "baud": uart_hub.baud(i), "rx": uart_hub.rx_total(i), "tx": uart_hub.tx_total(i)}
                 for i in range(config.UART_PORTS)],
    }


def _agent():
    a = agent.agent
    c = a.cfg
    p = next((x for x in c["profiles"] if x.get("name") == c["active"]), None)
    return {
        "busy": a.busy,
        "profile": c["active"], "provider": p and p.get("provider"), "model": p and p.get("model"),
        "auto_approve": c.get("auto_approve", False), "shell_mode": c.get("shell_mode", "ask"),
        "watches": len(c["watches"]), "watches_on": sum(1 for w in c["watches"] if w.get("enabled", True)),
        "alerts": len(a.alerts), "last_alert": a.alerts[-1] if a.alerts else None,
        "approvals": len(a.approvals), "messages": len(a.history), "pages": len(a.clients),
    }

# ---------- http ----------


async def h_page(req):
    return web.FileResponse(_PAGE, headers={"Content-Type": "text/html", "Cache-Control": "no-cache"})


async def h_api(req):
    host = await asyncio.to_thread(_host)
    services, netw = await asyncio.gather(asyncio.to_thread(_services), asyncio.to_thread(_net))
    return web.json_response({
        "time": time.time(),
        "host": host,
        "services": {"pi-tester": "active", **services},
        "net": netw,
        "i2c": await _i2c(req.query.get("i2c", "1") not in ("0", "")),
        **_bench(),
        "camera": await asyncio.to_thread(camera.cam.status),
        "agent": _agent(),
        "web": extra(),
    }, headers={"Cache-Control": "no-store"})


ROUTES = [web.get("/system", h_page), web.get("/api/system", h_api)]
