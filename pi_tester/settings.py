"""Station settings in /var/lib/pi-tester/settings.json (the P4 kept them in NVS)."""

import json
import os
import threading

from . import config

_PATH = os.path.join(config.STATE_DIR, "settings.json")
_lock = threading.Lock()

DEFAULTS = {
    # tester's own Ethernet address; DHCP when static_ip is false
    "static_ip": True,
    "ip": "192.168.10.2",
    "netmask": "255.255.255.0",
    "gateway": "",
    # DUT Redfish endpoint
    "dut_host": "192.168.1.100",
    "dut_https": True,
    "dut_user": "",
    "dut_pass": "",
    # Wi-Fi station; empty SSID = off
    "wifi_ssid": "",
    "wifi_pass": "",
}

_cfg = dict(DEFAULTS)


def init():
    global _cfg
    try:
        with open(_PATH) as f:
            _cfg = {**DEFAULTS, **{k: v for k, v in json.load(f).items() if k in DEFAULTS}}
    except FileNotFoundError:
        print("settings: no stored settings, using defaults", flush=True)
    except (OSError, ValueError):
        print("settings: stored settings unreadable, using defaults", flush=True)


def get():
    return dict(_cfg)


def save(new):
    global _cfg
    with _lock:
        os.makedirs(config.STATE_DIR, exist_ok=True)
        tmp = _PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(new, f, indent=2)
        os.chmod(tmp, 0o600)           # holds the DUT and Wi-Fi passwords
        os.replace(tmp, _PATH)
        _cfg = dict(new)
