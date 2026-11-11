"""Wi-Fi setup mode for a station without screen or keyboard, like an IoT device.

If no known Wi-Fi is joined for WIFI_SETUP_WAIT_S, the Pi opens its own open
network (WIFI_SETUP_SSID). A phone that joins it gets the setup page as a
captive portal (every DNS name points at the Pi), picks a network, types the
password and saves; the Pi closes the setup network and joins that one. If it
can't, setup mode comes back so the user can try again.

wlan0 is normally run by netplan (networkd + netplan-wpa-wlan0). Setup mode
stops that supplicant, gives wlan0 a fixed address and DHCP server through a
networkd file in /run that sorts before netplan's, and runs hostapd; leaving it
removes all of that and runs `netplan apply`.

Holding the setup button (WIFI_SETUP_BUTTON) for a few seconds starts setup
mode at once, also while the Pi boots; the board's green LED blinks fast while
setup mode is on."""

import asyncio
import codecs
import json
import os
import re
import socket
import struct
import subprocess
import time

import gpiod
from aiohttp import web
from gpiod.line import Bias, Direction, Value

from . import config, net, settings

_PAGE = os.path.join(os.path.dirname(__file__), "web", "setup.html")
_RUN_DIR = "/run/pi-tester"
_HOSTAPD_CONF = os.path.join(_RUN_DIR, "hostapd.conf")
_NETWORKD_FILE = "/run/systemd/network/05-pi-tester-setup.network"   # before 10-netplan-*
_WPA_UNIT = f"netplan-wpa-{config.WIFI_IF}.service"
_STATE = os.path.join(config.STATE_DIR, "wifi_setup.json")
AP_IP = config.WIFI_SETUP_IP


def _ap_ssid():
    if config.WIFI_SETUP_SSID:
        return config.WIFI_SETUP_SSID
    try:
        with open(f"/sys/class/net/{config.WIFI_IF}/address") as f:
            mac = f.read().strip().replace(":", "").upper()
    except OSError:
        mac = ""
    return f"M4013-Setup-{mac[-4:]}" if mac else "M4013-Setup"


def _run(*cmd, timeout=30):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _link():
    """SSID wlan0 has joined, "" if none."""
    m = re.search(r"^\s*SSID: (.*)$", _run("iw", "dev", config.WIFI_IF, "link", timeout=5).stdout, re.M)
    return _unescape(m.group(1)) if m else ""


def _unescape(s):
    """iw prints non-ASCII SSID bytes as \\xNN."""
    try:
        return codecs.escape_decode(s.encode())[0].decode("utf-8", "replace")
    except ValueError:
        return s


def _scan():
    """Networks in range, strongest first: [{ssid, signal, secure}]. Only works
    in client mode; the result is kept for the setup page."""
    for _ in range(3):
        r = _run("iw", "dev", config.WIFI_IF, "scan", timeout=20)
        if r.returncode == 0:
            break
        time.sleep(2)                                # -16 busy: the supplicant is scanning
    nets = {}
    for block in r.stdout.split("\nBSS ")[1:] if r.returncode == 0 else []:
        ssid = re.search(r"^\s*SSID: (.*)$", block, re.M)
        sig = re.search(r"signal: (-?[\d.]+) dBm", block)
        if not ssid or not ssid.group(1).strip():
            continue                                 # hidden network
        name = _unescape(ssid.group(1))
        n = {"ssid": name, "signal": round(float(sig.group(1))) if sig else -100,
             "secure": "RSN:" in block or "WPA:" in block or "Privacy" in block}
        if name not in nets or n["signal"] > nets[name]["signal"]:
            nets[name] = n
    return sorted(nets.values(), key=lambda n: -n["signal"])


def _led(blink):
    """Fast blink in setup mode, back to SD-card activity otherwise."""
    try:
        with open(f"{config.WIFI_SETUP_LED}/trigger", "w") as f:
            f.write("timer" if blink else "mmc0")
        if blink:
            for k in ("delay_on", "delay_off"):
                with open(f"{config.WIFI_SETUP_LED}/{k}", "w") as f:
                    f.write("100")
    except OSError:
        pass


def _stations():
    return _run("iw", "dev", config.WIFI_IF, "station", "dump", timeout=5).stdout.count("Station ")


class _Dns(asyncio.DatagramProtocol):
    """Answers every A query with the Pi's address, so any page the phone opens,
    including its captive-portal check, lands on the setup page."""

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if len(data) < 12:
            return
        qid, flags, qdcount = struct.unpack(">HHH", data[:6])
        if flags & 0x8000 or qdcount != 1:
            return
        i = 12
        while i < len(data) and data[i]:
            i += 1 + data[i]
        if i + 5 > len(data):
            return
        question = data[12:i + 5]
        qtype = struct.unpack(">H", data[i + 1:i + 3])[0]
        answer = b""
        if qtype == 1:                               # A; AAAA and the rest get no answer
            answer = b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 10, 4) + socket.inet_aton(AP_IP)
        header = struct.pack(">HHHHHH", qid, 0x8080 | (flags & 0x0100), 1, 1 if answer else 0, 0, 0)
        self.transport.sendto(header + question + answer, addr)


class WifiSetup:
    def __init__(self):
        self.active = False                          # setup network is up
        self.since = 0.0
        self.networks = []                           # last scan, for the page
        self.pending = None                          # (ssid, password) saved, not yet applied
        self.trying = ""                             # SSID being joined after a save
        self.last = self._load()                     # result of the last save
        self._hostapd = None
        self._dns = None
        self._down_since = None

    # ---------- state ----------

    def _load(self):
        try:
            with open(_STATE) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _remember(self, ssid, ok, msg):
        self.last = {"ssid": ssid, "ok": ok, "msg": msg, "time": time.time()}
        try:
            os.makedirs(config.STATE_DIR, exist_ok=True)
            with open(_STATE, "w") as f:
                json.dump(self.last, f)
        except OSError:
            pass
        print(f"wifi-setup: {msg}", flush=True)

    def status(self):
        return {"active": self.active, "ap_ssid": _ap_ssid(), "ap_ip": AP_IP,
                "since": self.since if self.active else None,
                "clients": _stations() if self.active else 0,
                "networks": self.networks, "trying": self.trying, "last": self.last,
                "known": settings.get()["wifi_ssid"]}

    # ---------- switching ----------

    def _start_ap(self):
        """Blocking; runs in a thread."""
        self.networks = _scan() or self.networks
        os.makedirs(_RUN_DIR, exist_ok=True)
        with open(_HOSTAPD_CONF, "w") as f:
            f.write(f"interface={config.WIFI_IF}\ndriver=nl80211\nssid2=\"{_ap_ssid()}\"\n"
                    f"hw_mode=g\nchannel={config.WIFI_SETUP_CHANNEL}\ncountry_code={config.WIFI_COUNTRY}\n"
                    "ieee80211d=1\nwmm_enabled=1\nauth_algs=1\nignore_broadcast_ssid=0\n")
        with open(_NETWORKD_FILE, "w") as f:
            f.write(f"[Match]\nName={config.WIFI_IF}\n\n[Network]\nAddress={AP_IP}/24\n"
                    "ConfigureWithoutCarrier=yes\nDHCPServer=yes\nLinkLocalAddressing=no\n\n"
                    f"[DHCPServer]\nPoolOffset=10\nPoolSize=50\nEmitDNS=yes\nDNS={AP_IP}\n"
                    "EmitRouter=yes\nDefaultLeaseTimeSec=300\n")
        _run("systemctl", "stop", _WPA_UNIT)
        self._hostapd = subprocess.Popen(["hostapd", _HOSTAPD_CONF])
        _run("networkctl", "reload")
        _run("networkctl", "reconfigure", config.WIFI_IF)
        for _ in range(50):                          # the address is needed to bind DNS
            if net.ipv4(config.WIFI_IF) == AP_IP:
                break
            if self._hostapd.poll() is not None:
                raise OSError(f"hostapd exited with {self._hostapd.returncode}")
            time.sleep(0.2)
        else:
            raise OSError(f"{config.WIFI_IF} did not get {AP_IP}")

    def _stop_ap(self):
        """Blocking; back to netplan's client mode, also after a crash or a restart
        that left setup mode half on."""
        if self._hostapd:
            self._hostapd.terminate()
            try:
                self._hostapd.wait(5)
            except subprocess.TimeoutExpired:
                self._hostapd.kill()
            self._hostapd = None
        _run("pkill", "-f", f"hostapd {_HOSTAPD_CONF}")
        try:
            os.remove(_NETWORKD_FILE)
        except FileNotFoundError:
            pass
        r = _run("netplan", "apply", timeout=60)     # restarts the supplicant, reloads networkd
        if r.returncode != 0:
            print(f"wifi-setup: netplan apply failed: {r.stderr.strip()[:200]}", flush=True)
            _run("systemctl", "start", _WPA_UNIT)

    async def start(self, why):
        if self.active:
            return
        print(f"wifi-setup: starting setup network {_ap_ssid()} ({why})", flush=True)
        _led(True)
        try:
            await asyncio.to_thread(self._start_ap)
            loop = asyncio.get_running_loop()
            self._dns, _ = await loop.create_datagram_endpoint(_Dns, local_addr=(AP_IP, 53))
        except (OSError, subprocess.SubprocessError) as e:
            print(f"wifi-setup: could not start: {e}", flush=True)
            await self.stop()
            return
        self.active, self.since = True, time.time()

    async def stop(self):
        if self._dns:
            self._dns.close()
            self._dns = None
        was = self.active
        self.active = False
        await asyncio.to_thread(self._stop_ap)
        _led(False)
        self._down_since = time.monotonic()          # known networks get a fresh WAIT_S
        if was:
            print("wifi-setup: setup network closed", flush=True)

    async def save(self, ssid, password):
        """Stores the network the same way the Settings page does, then leaves
        setup mode a moment later so the page can still show that it worked."""
        s = settings.get()
        s.update(wifi_ssid=ssid, wifi_pass=password)
        await asyncio.to_thread(net.write_netplan, s)
        settings.save(s)
        self.pending = ssid

    # ---------- button ----------

    async def button(self):
        try:
            await self._button()
        except Exception as e:                       # report it, the rest keeps running
            print(f"wifi-setup: button stopped: {e!r}", flush=True)

    async def _button(self):
        """Polls the button (pull-up, pressed = low); a hold of HOLD_S starts
        setup mode. Held through boot, it triggers as soon as the service runs."""
        pin = config.WIFI_SETUP_BUTTON
        req = gpiod.request_lines(config.GPIO_CHIP, consumer="pi-tester-setup", config={
            pin: gpiod.LineSettings(direction=Direction.INPUT, bias=Bias.PULL_UP)})
        print(f"wifi-setup: button on GPIO{pin}, hold {config.WIFI_SETUP_BUTTON_HOLD_S} s", flush=True)
        pressed_at = None
        while True:
            await asyncio.sleep(0.1)
            if req.get_value(pin) == Value.INACTIVE:
                pressed_at = pressed_at or time.monotonic()
                if time.monotonic() - pressed_at >= config.WIFI_SETUP_BUTTON_HOLD_S and not self.active:
                    await self.start("button held")
                    pressed_at = float("inf")    # once per press
            else:
                pressed_at = None

    # ---------- watcher ----------

    async def run(self):
        if os.path.exists(_NETWORKD_FILE):           # left over from a restart in setup mode
            await asyncio.to_thread(self._stop_ap)
        _led(False)
        self._down_since = time.monotonic()
        while True:
            await asyncio.sleep(config.WIFI_SETUP_POLL_S)
            try:
                await self._tick()
            except Exception as e:                   # never let the watcher die
                print(f"wifi-setup: {e!r}", flush=True)

    async def _tick(self):
        now = time.monotonic()
        if self.active:
            if self.pending:
                await asyncio.sleep(3)               # let the page get its answer
                self.trying, self.pending = self.pending, None
                await self.stop()
            elif (time.time() - self.since > config.WIFI_SETUP_RETRY_S
                  and await asyncio.to_thread(_stations) == 0):
                await self.stop()                    # nobody came: try the known networks again
            return
        joined = await asyncio.to_thread(_link)
        if joined:
            self._down_since = None
            if self.trying:
                self._remember(self.trying, True, f"joined {self.trying}")
                self.trying = ""
            return
        if self._down_since is None:
            self._down_since = now
        elif now - self._down_since > config.WIFI_SETUP_WAIT_S:
            if self.trying:
                self._remember(self.trying, False,
                               f"could not join {self.trying}: wrong password or out of range")
                self.trying = ""
            await self.start("no known Wi-Fi")


setup = WifiSetup()

# ---------- http ----------


def _on_ap(req):
    sock = req.transport and req.transport.get_extra_info("sockname")
    return bool(sock) and sock[0] == AP_IP


@web.middleware
async def captive(req, handler):
    """On the setup network, any other host name (the phone's portal check,
    a page the user types) goes to the setup page."""
    if _on_ap(req) and (req.host.split(":")[0] != AP_IP or req.path == "/"):
        raise web.HTTPFound(f"http://{AP_IP}/setup")
    return await handler(req)


async def h_page(req):
    return web.FileResponse(_PAGE, headers={"Content-Type": "text/html", "Cache-Control": "no-cache"})


async def h_get(req):
    return web.json_response(await asyncio.to_thread(setup.status), headers={"Cache-Control": "no-store"})


async def h_post(req):
    try:
        j = await req.json()
    except ValueError:
        j = None
    if not isinstance(j, dict):
        return web.json_response({"ok": False, "error": "bad JSON"})
    action = j.get("action", "save")
    if action == "start":
        await setup.start("asked for")
        return web.json_response({"ok": setup.active, **({} if setup.active else {"error": "could not start, see the log"})})
    if action == "stop":
        await setup.stop()
        return web.json_response({"ok": True})
    if not setup.active:
        return web.json_response({"ok": False, "error": "setup mode is not on; use Settings"})
    ssid, password = j.get("ssid"), j.get("password", "")
    if not isinstance(ssid, str) or not 1 <= len(ssid.encode()) <= 32:
        return web.json_response({"ok": False, "error": "network name must be 1 to 32 bytes"})
    if not isinstance(password, str) or (password and not 8 <= len(password) <= 63):
        return web.json_response({"ok": False, "error": "password must be 8 to 63 characters, or empty for an open network"})
    try:
        await setup.save(ssid, password)
    except (ValueError, OSError) as e:
        return web.json_response({"ok": False, "error": str(e)})
    return web.json_response({"ok": True})


async def on_startup(app):
    if config.WIFI_SETUP:
        app["wifi_setup"] = asyncio.create_task(setup.run())
        if config.WIFI_SETUP_BUTTON is not None:
            app["wifi_setup_button"] = asyncio.create_task(setup.button())


ROUTES = [web.get("/setup", h_page), web.get("/api/wifi-setup", h_get), web.post("/api/wifi-setup", h_post)]
