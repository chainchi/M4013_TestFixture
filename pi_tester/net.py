"""Ethernet / Wi-Fi status, and the station network settings written as netplan.
Network changes take effect after a reboot, as on the P4."""

import glob
import ipaddress
import json
import os
import re
import shutil
import subprocess

from . import config

_BACKUP_DIR = os.path.join(config.STATE_DIR, "netplan-backup")
_CLOUD_INIT_OFF = "/etc/cloud/cloud.cfg.d/99-pi-tester-network.cfg"
NETWORK_KEYS = ("static_ip", "ip", "netmask", "gateway", "wifi_ssid", "wifi_pass")


def _run(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout


def link_up(ifname=config.ETH_IF):
    try:
        with open(f"/sys/class/net/{ifname}/carrier") as f:
            return f.read().strip() == "1"
    except OSError:
        return False


def ipv4(ifname):
    """First IPv4 address as text, "0.0.0.0" until one is assigned."""
    try:
        for a in json.loads(_run("ip", "-4", "-j", "addr", "show", "dev", ifname))[0]["addr_info"]:
            return a["local"]
    except (ValueError, IndexError, KeyError, OSError, subprocess.SubprocessError):
        pass
    return "0.0.0.0"


def wifi_status():
    """(connected, rssi dBm)."""
    try:
        out = _run("iw", "dev", config.WIFI_IF, "link")
    except (OSError, subprocess.SubprocessError):
        return False, 0
    m = re.search(r"signal:\s*(-?\d+)", out)
    return out.startswith("Connected"), int(m.group(1)) if m else 0


def _netplan(s):
    eth = {"optional": True, "link-local": ["ipv6"]}
    if s["static_ip"]:
        prefix = ipaddress.IPv4Network(f"0.0.0.0/{s['netmask']}").prefixlen
        eth.update({"dhcp4": False, "addresses": [f"{s['ip']}/{prefix}"]})
        if s["gateway"]:
            eth["routes"] = [{"to": "default", "via": s["gateway"]}]
    else:
        eth["dhcp4"] = True
    net = {"version": 2, "ethernets": {config.ETH_IF: eth}}
    if s["wifi_ssid"]:
        ap = {"password": s["wifi_pass"]} if s["wifi_pass"] else {}
        net["wifis"] = {config.WIFI_IF: {"optional": True, "dhcp4": True,
                                         "regulatory-domain": config.WIFI_COUNTRY,
                                         "access-points": {s["wifi_ssid"]: ap}}}
    return {"network": net}


def write_netplan(s):
    """Makes NETPLAN_FILE the only netplan config (others are moved to the
    backup dir, cloud-init stops regenerating its own) and validates it.
    Raises ValueError with netplan's message if the config is rejected."""
    if s["static_ip"]:
        ipaddress.IPv4Address(s["ip"])
        if s["gateway"]:
            ipaddress.IPv4Address(s["gateway"])
    os.makedirs(_BACKUP_DIR, exist_ok=True)
    moved = []
    for f in glob.glob("/etc/netplan/*.yaml"):
        if f != config.NETPLAN_FILE:
            dst = os.path.join(_BACKUP_DIR, os.path.basename(f))
            shutil.move(f, dst)
            moved.append((dst, f))
    old = None
    if os.path.exists(config.NETPLAN_FILE):
        with open(config.NETPLAN_FILE) as f:
            old = f.read()
    # JSON is valid YAML
    fd = os.open(config.NETPLAN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(_netplan(s), f, indent=2)
    r = subprocess.run(["netplan", "generate"], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        if old is None:
            os.remove(config.NETPLAN_FILE)
        else:
            with open(config.NETPLAN_FILE, "w") as f:
                f.write(old)
        for dst, f in moved:
            shutil.move(dst, f)
        raise ValueError(f"netplan rejected the settings: {r.stderr.strip()[:200]}")
    os.makedirs(os.path.dirname(_CLOUD_INIT_OFF), exist_ok=True)
    with open(_CLOUD_INIT_OFF, "w") as f:
        f.write("network: {config: disabled}\n")
