#!/usr/bin/env python3
"""Fleet debug: SSH health checks and log surveys on the systems (DUTs) listed on
the Pi's /fleet page.

The list is ~/fleet/systems.json (mode 600), edited on the /fleet page form;
results (Status, Last check, Flags, CPER, Summary) are written back into it.

    fleet.py list                   the systems (passwords never shown)
    fleet.py check [NAME ...]       health check on all or the named systems:
                                    report in ~/fleet/reports/<name>/, results
                                    written back to the list
    fleet.py run NAME [--sudo] -- CMD          run one command, print its output
    fleet.py collect NAME KEY [--sudo] -- CMD  run one command, save the FULL output
                                    to reports/<name>/raw/KEY.txt
    fleet.py note NAME TEXT         write TEXT to the system's Summary
    fleet.py forget NAME            drop its saved host key (system was replaced)
    fleet.py hmc-probe NAME         BMC row: is its HMC alive (ping) and answering Redfish
    fleet.py export FILE.xlsx       the list with results as Excel, without passwords
    fleet.py edit < JSON            add/change/delete a system (used by the web page)

Access "console": SSH lands on a serial console (a shell with no login), as on
the GPU trays. Commands are then typed into that shell one at a time, slowly,
between echo markers; credentials are never typed into a console.

BMC rows (Type bmc or Profile bmc-openbmc) also read the HMC behind the BMC,
through the BMC, with Redfish GET requests only.

Passwords stay in systems.json: they are only sent to the system being logged
in to (and to its sudo with --sudo), never printed or written to reports.
"""

import argparse
import base64
import concurrent.futures
import datetime
import json
import os
import re
import shlex
import socket
import sys
import threading
import time

FLEET_DIR = os.environ.get("FLEET_DIR", os.path.expanduser("~/fleet"))
STORE = os.environ.get("FLEET_STORE", os.path.join(FLEET_DIR, "systems.json"))
REPORTS = os.path.join(FLEET_DIR, "reports")
KNOWN_HOSTS = os.path.join(FLEET_DIR, "known_hosts")
PROGRESS = os.path.join(FLEET_DIR, ".progress")      # live state per system, shown on /fleet
ACCESS = ("ssh", "console")
HMC_DEFAULT = "172.31.13.251"
PROFILES = ("linux", "gpu-vera", "bmc-openbmc")
CONNECT_TIMEOUT = 15
TIMEOUT = {"ssh": 30, "console": 300}
WORKERS = 8

# Read-only checks. Each runs on its own; a failing one does not stop the rest.
CHECKS = [
    ("identity", "hostname; cat /etc/os-release 2>/dev/null | grep -E '^(PRETTY_NAME|VERSION)='; uname -r"),
    ("uptime", "uptime; echo; last -x reboot 2>/dev/null | head -n 3"),
    ("cpu", "nproc; cat /proc/loadavg; echo; (ps -eo pid,user,comm,%cpu,%mem --sort=-%cpu 2>/dev/null || top -bn1 2>/dev/null) | head -n 12"),
    ("memory", "free -m"),
    ("disk", "df -hP -x tmpfs -x devtmpfs -x squashfs -x overlay 2>/dev/null || df -h; "   # || BusyBox
             "echo \"read-only: $(awk '$4 ~ /^ro(,|$)/ {print $2}' /proc/mounts | tr '\n' ' ')\""),
    ("inodes", "df -iP -x tmpfs -x devtmpfs -x squashfs -x overlay 2>/dev/null || df -i 2>/dev/null"),
    ("failed_units", "systemctl --failed --no-pager --no-legend 2>&1"),
    ("errors_this_boot", "echo \"error lines this boot: $(journalctl -p err -b --no-pager -q 2>/dev/null | wc -l)\"; "
                         "journalctl -p err -b --no-pager -q 2>&1 | tail -n 40"),
    ("kernel_warnings", "journalctl -k -p warning -b --no-pager -q 2>&1 | tail -n 40 || dmesg --level=err,warn 2>&1 | tail -n 40"),
    ("network", "ip -br addr; echo; ip route; echo; cat /etc/resolv.conf 2>/dev/null | grep -v '^#'"),
    ("listening", "ss -tulnH 2>/dev/null | head -n 30"),
    ("time", "timedatectl 2>/dev/null | grep -E 'Local time|synchronized|NTP' || date"),
    ("storage", "lsblk -o NAME,SIZE,TYPE,MOUNTPOINT 2>/dev/null"),
    ("hardware", "cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null | head -n 8; command -v sensors >/dev/null && sensors 2>/dev/null | head -n 40"),
]

_KLOG = "journalctl -k -b 0 --no-pager -q 2>/dev/null"
GPU_CHECKS = [
    ("gpu_smi", "nvidia-smi --query-gpu=index,pci.bus_id,name,serial,power.draw,power.limit,temperature.gpu,"
                "memory.total,ecc.errors.uncorrected.volatile.total,ecc.errors.corrected.volatile.total,"
                "pcie.link.gen.current,pcie.link.width.current --format=csv 2>&1"),
    ("gpu_smi_head", "nvidia-smi 2>&1 | sed -n 1,4p"),
    ("gpu_nvlink", "nvidia-smi nvlink -s 2>&1 | head -n 80"),
    ("gpu_drivers", "echo \"running: $(cat /proc/driver/nvidia/version 2>&1 | head -n 1)\"; "
                    "echo \"module file: $(modinfo -F version nvidia 2>&1)\"; "
                    "dpkg -l 2>/dev/null | awk '/^ii/ && /nvidia-driver|nvidia-kernel|driver-local-repo/ {print \"package: \" $2, $3}'"),
    ("gpu_fw_files", "ls /lib/firmware/nvidia/ 2>&1"),
    ("kern_gpu", f"journalctl -k -b 0 --no-pager -q -o short-iso 2>/dev/null | grep -E 'NVRM|Xid|GSP|Direct firmware load|nvidia' | tail -n 80"),
    ("kern_hwerr_head", f"{_KLOG} | grep 'Hardware Error' | grep -vE '\\]:   [0-9a-f]{{8}}:' | head -n 80"),
    ("kern_pcie", f"{_KLOG} | grep -iE 'AER|pcieport|ATS|DPC|link down|downtrain' | tail -n 60"),
    ("pcie_gpu", "lspci -d 10de: -nn 2>&1; for d in $(lspci -D -d 10de: 2>/dev/null | cut -d' ' -f1); do "
                 "echo \"$d $(cat /sys/bus/pci/devices/$d/current_link_speed 2>/dev/null) "
                 "x$(cat /sys/bus/pci/devices/$d/current_link_width 2>/dev/null)\"; done"),
    ("nic", "ip -br link; ibstat 2>/dev/null | grep -E \"CA '|State|Rate\"; command -v mlxlink >/dev/null && "
            "for d in $(ls /sys/class/infiniband 2>/dev/null); do echo \"== $d\"; mlxlink -d $d -m 2>&1 | grep -E 'State|Speed|Width'; done"),
    ("mctp_host", "zgrep -h CONFIG_MCTP= /proc/config.gz /boot/config-$(uname -r) 2>/dev/null; "
                  "command -v mctp >/dev/null && mctp link; ls /sys/class/net | grep -i mctp"),
    ("modules_bad", f"{_KLOG} | grep -E 'disagrees about version|Unknown symbol' | sort -u | head -n 20"),
    ("boot_time", "echo \"now $(date +%s) uptime $(cut -d' ' -f1 /proc/uptime) first "
                  "$(journalctl -b 0 -o short-unix --no-pager -q 2>/dev/null | head -n 1 | cut -d' ' -f1)\"; "
                  "journalctl -b 0 -o short-iso --no-pager -q 2>/dev/null | head -n 1; "
                  "journalctl -b 0 -o short-iso --no-pager -q -n 1 2>/dev/null"),
]
# Saved in full to reports/<name>/raw/<key>.txt (CPER hex dumps must be complete).
_HWERR = "grep 'Hardware Error' | sed -E 's/^.*kernel: //'"
GPU_RAW = [
    ("hwerr", f"{_KLOG} | {_HWERR}"),
    ("hwerr_prev", f"journalctl --list-boots --no-pager -q 2>/dev/null | grep -qE '^ *-1 ' && "
                   f"journalctl -k -b -1 --no-pager -q 2>/dev/null | {_HWERR}"),
]


def die(msg):
    print(f"fleet: {msg}", file=sys.stderr)
    sys.exit(2)


def _dirname(name):
    return os.path.join(REPORTS, re.sub(r"[^A-Za-z0-9_.-]", "_", name))


# ---------- system list (systems.json, edited on the /fleet page) ----------

# input fields: (key, Excel export header)
FIELDS = [("name", "Name"), ("ip", "IP"), ("port", "Port"), ("user", "User"), ("access", "Access"),
          ("profile", "Profile"), ("type", "Type"), ("server", "Server"), ("hmc", "HMC IP"),
          ("rack", "Rack/Slot"), ("stage", "Stage"), ("timeout", "Timeout"), ("expected_gpus", "Expected GPUs"),
          ("expected_pcie", "Expected PCIe"), ("expected_nvlink", "Expected NVLink"),
          ("expected_fw", "Expected FW"), ("problem", "Problem")]
RESULTS = {"Status": "status", "Last check": "last", "Flags": "flags", "CPER": "cper", "Summary": "summary"}
CHOICES = {"access": ACCESS, "profile": PROFILES, "type": ("", "host", "bmc"),
           "stage": ("", "ES", "QS", "PS", "MP"), "expected_nvlink": ("", "yes", "no")}


class _Locked:
    """flock on ~/fleet/.systems.lock around every read-modify-write of systems.json
    (the web form and a running check write the same file)."""
    def __enter__(self):
        import fcntl
        os.makedirs(FLEET_DIR, exist_ok=True)
        self.f = open(os.path.join(FLEET_DIR, ".systems.lock"), "a")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        self.f.close()


def _read_store():
    if not os.path.exists(STORE):
        _import_xlsx()
    try:
        with open(STORE) as f:
            return json.load(f).get("systems", [])
    except FileNotFoundError:
        return []
    except ValueError as e:
        die(f"{STORE} is not valid JSON ({e})")


def _write_store(recs):
    tmp = STORE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"systems": recs}, f, indent=1)
    os.replace(tmp, STORE)


def _import_xlsx():
    """One-time move from the old Excel workbook (systems.xlsx) to systems.json."""
    old = os.path.join(FLEET_DIR, "systems.xlsx")
    if not os.path.exists(old):
        return
    import openpyxl
    wb = openpyxl.load_workbook(old, data_only=True)
    ws = wb["Systems"] if "Systems" in wb.sheetnames else wb.worksheets[0]
    head = [str(c.value or "").strip().lower() for c in ws[1]]
    hdr = {h.lower(): k for k, h in FIELDS + [("password", "Password")]}
    hdr.update({h.lower(): k for h, k in RESULTS.items()})
    recs = []
    for vals in ws.iter_rows(min_row=2, values_only=True):
        r = {}
        for h, v in zip(head, vals):
            if h in hdr:
                k = hdr[h]
                r[k] = "" if v is None else (str(v) if k == "password" else str(v).strip())
        if r.get("name") or r.get("ip"):
            r["name"] = r.get("name") or r["ip"]
            recs.append(r)
    _write_store(recs)
    os.replace(old, old + ".imported")
    print(f"fleet: moved {len(recs)} system(s) from {old} to {STORE}", file=sys.stderr)


def _num(v, what, lo, hi):
    if v is None or str(v).strip() == "":
        return ""
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        raise ValueError(f"{what} '{v}' is not a number")
    if not lo <= n <= hi:
        raise ValueError(f"{what} must be {lo}..{hi}")
    return n


_HOST = re.compile(r"[A-Za-z0-9_.:-]{1,253}")


def clean(d):
    """Validate one system as entered on the form -> stored record (raises ValueError)."""
    r = {k: ("" if d.get(k) is None else str(d.get(k)).strip()) for k, _ in FIELDS}
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", r["name"]):
        raise ValueError("Name: 1-40 letters, digits, - _ .")
    if not _HOST.fullmatch(r["ip"]):
        raise ValueError("IP: an address or host name")
    if r["hmc"] and r["hmc"].lower() != "none" and not _HOST.fullmatch(r["hmc"]):
        raise ValueError(f"HMC IP: an address, empty (= {HMC_DEFAULT}) or none")
    r["port"] = _num(r["port"], "Port", 1, 65535)
    r["timeout"] = _num(r["timeout"], "Timeout", 1, 3600)
    r["expected_gpus"] = _num(r["expected_gpus"], "Expected GPUs", 0, 64)
    for k in ("access", "profile", "type", "expected_nvlink"):
        r[k] = r[k].lower()
    r["stage"] = r["stage"].upper()
    r["access"] = r["access"] or "ssh"
    r["profile"] = r["profile"] or "linux"
    for k, ok in CHOICES.items():
        if r[k] not in ok:
            raise ValueError(f"{k}: one of {', '.join(x or '(empty)' for x in ok)}")
    for k in ("server", "rack", "expected_pcie", "expected_fw", "user"):
        if len(r[k]) > 60:
            raise ValueError(f"{k}: at most 60 characters")
    r["problem"] = r["problem"][:500]
    return r


def _toi(v):
    return int(v) if str(v).strip().isdigit() else None


def _runtime(rec):
    """Stored record -> the dict the checks use (defaults filled in)."""
    access = rec.get("access") or "ssh"
    hmc = str(rec.get("hmc") or "")
    nvl = str(rec.get("expected_nvlink") or "").lower()
    return {"name": rec["name"], "ip": rec.get("ip", ""), "port": _toi(rec.get("port")) or 22,
            "user": rec.get("user") or "root", "password": rec.get("password") or "",
            "access": access, "profile": rec.get("profile") or "linux", "type": (rec.get("type") or "").lower(),
            "server": rec.get("server", ""), "rack": rec.get("rack", ""), "stage": (rec.get("stage") or "").upper(),
            "hmc": "" if hmc.lower() in ("none", "no", "-", "skip") else hmc or HMC_DEFAULT,
            "timeout": _toi(rec.get("timeout")) or TIMEOUT[access],
            "expected_gpus": _toi(rec.get("expected_gpus")), "expected_pcie": rec.get("expected_pcie", ""),
            "expected_nvlink": True if nvl in ("yes", "y", "true", "1") else
                               False if nvl in ("no", "n", "false", "0") else None,
            "expected_fw": rec.get("expected_fw", ""), "problem": rec.get("problem", ""),
            "status": rec.get("status", ""), "last": rec.get("last", ""), "summary": rec.get("summary", "")}


def load():
    """-> [system dicts] in list order."""
    with _Locked():
        recs = _read_store()
    return [_runtime(r) for r in recs if r.get("name")]


def find(rows, name):
    for s in rows:
        if s["name"] == name or s["ip"] == name:
            return s
    die(f"no system '{name}' (fleet.py list shows them; add systems on the /fleet page)")


def write_back(updates):
    """updates: {name: {"Status"|"Last check"|"Flags"|"CPER"|"Summary": value}}, under the
    lock so edits made on the web page meanwhile are kept."""
    with _Locked():
        recs = _read_store()
        for r in recs:
            for k, v in updates.get(r.get("name"), {}).items():
                r[RESULTS[k]] = v
        _write_store(recs)


def cmd_edit():
    """stdin JSON from the web page:
    {"op": "upsert", "old_name": NAME|null, "system": {...}, "password": "..." (absent = keep),
     "clear_password": bool}  or  {"op": "delete", "name": NAME}."""
    req = json.load(sys.stdin)
    with _Locked():
        recs = _read_store()
        names = [r.get("name") for r in recs]
        if req.get("op") == "delete":
            if req.get("name") not in names:
                die(f"no system '{req.get('name')}'")
            _write_store([r for r in recs if r.get("name") != req["name"]])
            print(json.dumps({"ok": True}))
            return
        if req.get("op") != "upsert":
            die("op: upsert or delete")
        try:
            new = clean(req.get("system") or {})
        except ValueError as e:
            die(str(e))
        old = req.get("old_name") or None
        if old and old not in names:
            die(f"no system '{old}' (deleted meanwhile?)")
        if new["name"] in names and new["name"] != old:
            die(f"a system named '{new['name']}' already exists")
        if not old and len(recs) >= 500:
            die("at most 500 systems")
        prev = recs[names.index(old)] if old else {}
        rec = dict(prev, **new)
        if req.get("clear_password"):
            rec["password"] = ""
        elif req.get("password") is not None:
            rec["password"] = str(req["password"])[:200]
        if prev and any(str(prev.get(k, "")) != str(new[k]) for k in ("ip", "port", "user", "access")):
            rec.update(status="", flags="", cper="")      # login changed: the old result no longer applies
        if old:
            recs[names.index(old)] = rec
        else:
            recs.append(rec)
        _write_store(recs)
    print(json.dumps({"ok": True, "name": rec["name"]}))


def export_xlsx(path):
    """The list with results as an Excel file, WITHOUT passwords (download on /fleet)."""
    import openpyxl
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter
    with _Locked():
        recs = _read_store()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Systems"
    head = [h for _, h in FIELDS] + ["Password set"] + list(RESULTS)
    ws.append(head)
    for r in recs:
        ws.append([r.get(k, "") for k, _ in FIELDS] + ["yes" if r.get("password") else ""] +
                  [r.get(k, "") for k in RESULTS.values()])
    for i, h in enumerate(head, 1):
        ws.column_dimensions[get_column_letter(i)].width = 60 if h in ("Flags", "Summary") else max(9, len(h) + 2)
        ws.cell(row=1, column=i).font = Font(bold=True)
    ws.freeze_panes = "B2"
    wb.save(path)


# ---------- ssh ----------

def connect(s):
    import paramiko
    c = paramiko.SSHClient()
    os.makedirs(FLEET_DIR, exist_ok=True)
    if os.path.exists(KNOWN_HOSTS):
        c.load_host_keys(KNOWN_HOSTS)
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())   # first contact: remember the key
    c.connect(s["ip"], port=s["port"], username=s["user"], password=s["password"] or None,
              timeout=CONNECT_TIMEOUT, auth_timeout=CONNECT_TIMEOUT, banner_timeout=CONNECT_TIMEOUT,
              look_for_keys=not s["password"], allow_agent=False)
    c.save_host_keys(KNOWN_HOSTS)
    os.chmod(KNOWN_HOSTS, 0o600)
    return c


class ConsoleLogin(Exception):
    """The serial console shows a login/password prompt; we never type credentials there."""


class SSHSession:
    """Normal SSH: one exec channel per command."""
    console = False

    def __init__(self, c):
        self.c = c

    def run(self, cmd, timeout, password=None):
        """-> (exit code, combined output). With password: run under sudo -S."""
        full = f"exec 2>&1; LC_ALL=C; export LC_ALL; {cmd}"     # stderr merged by the shell
        if password is not None:
            full = f"sudo -S -p '' sh -c {shlex.quote(full)}"
        stdin, stdout, stderr = self.c.exec_command(full, timeout=timeout, get_pty=False)
        if password is not None:
            stdin.write(password + "\n")
            stdin.flush()
        stdin.channel.shutdown_write()
        try:
            out = stdout.read().decode("utf-8", "replace")
            out += stderr.read().decode("utf-8", "replace")     # only sudo's own messages
            rc = stdout.channel.recv_exit_status()
        except socket.timeout:
            return 124, f"(timed out after {timeout}s)"
        return rc, out

    def close(self):
        self.c.close()


# OSC (the host sends ESC]3008;...ESC\), CSI, charset and keypad-mode escapes
_ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][0-9A-Za-z]|\x1b[=>]")


class ConsoleSession:
    """SSH that lands on a serial-console shell: exec_command returns nothing there,
    so each command is typed into the PTY, slowly (serial input drops characters
    when sent fast), and its output is cut out between two echo markers. One
    command at a time: the next one is typed only after the previous end marker."""
    console = True
    CHUNK, GAP = 32, 0.1                            # ~320 characters per second

    def __init__(self, c):
        self.c, self.n = c, 0
        self.ch = c.invoke_shell(term="dumb", width=250, height=50)
        text = self._read_for(3.0)
        self._send("\r")
        text += self._read_for(2.0)
        lines = [l.strip() for l in self._clean(text).split("\n") if l.strip()]
        if lines and re.search(r"(login|password)\s*:$", lines[-1], re.I):
            raise ConsoleLogin(lines[-1][-60:])

    @staticmethod
    def _clean(t):
        return _ANSI.sub("", t).replace("\r", "")

    def _send(self, s):
        for i in range(0, len(s), self.CHUNK):
            self.ch.sendall(s[i:i + self.CHUNK].encode())
            time.sleep(self.GAP)

    def _recv(self):
        return self.ch.recv(65536).decode("utf-8", "replace") if self.ch.recv_ready() else ""

    def _read_for(self, secs):
        end, out = time.monotonic() + secs, []
        while time.monotonic() < end:
            d = self._recv()
            if d:
                out.append(d)
            else:
                time.sleep(0.1)
        return "".join(out)

    def run(self, cmd, timeout, password=None):
        """password is ignored: the console shell is already logged in, nothing is typed for sudo."""
        self.n += 1
        tag = f"{self.n}x{os.getpid()}"
        # The script goes base64-encoded (no newlines or quotes to mangle on the console), and
        # the typed line has "===B""tag" (quotes split it) while the output has ===Btag: the
        # echoed command line never matches a marker. | cat: no pager, stdout is not a tty.
        inner = f'( {cmd}\n) </dev/null; echo "===E{tag}" $?\n'
        b64 = base64.b64encode(inner.encode()).decode()
        line = (f'echo "===B""{tag}"; echo {b64} | base64 -d | '
                f'LC_ALL=C PAGER=cat SYSTEMD_PAGER=cat SYSTEMD_COLORS=0 sh 2>&1 | cat\r')
        b = re.compile(rf"===B{tag}[ \t]*$", re.M)
        e = re.compile(rf"===E{tag} (\d+)[ \t]*$", re.M)
        self._send(line)
        buf, end = "", time.monotonic() + timeout
        while time.monotonic() < end:
            d = self._recv()
            if not d:
                if self.ch.closed or self.ch.exit_status_ready():
                    break
                time.sleep(0.1)
                continue
            buf += d
            txt = self._clean(buf)
            me = e.search(txt)
            if me:
                mb = b.search(txt, 0, me.start())
                return int(me.group(1)), txt[mb.end() if mb else 0:me.start()].strip("\n")
        try:
            self._send("\x03")                      # Ctrl-C the command, back to the prompt
            self._read_for(2.0)
        except OSError:
            pass
        txt = self._clean(buf)
        mb = b.search(txt)
        part = txt[mb.end():].strip("\n") if mb else ""
        return 124, (part + "\n" if part else "") + f"(timed out after {timeout}s on the console)"

    def close(self):
        try:
            self.ch.close()
        finally:
            self.c.close()


def open_session(s):
    c = connect(s)
    if s["access"] != "console":
        return SSHSession(c)
    try:
        return ConsoleSession(c)
    except Exception:
        c.close()
        raise


def classify(err):
    import paramiko
    if isinstance(err, ConsoleLogin):
        return "CONSOLE LOGIN", (f"the console shows a login prompt ('{err}'); fleet.py never types credentials "
                                 "into a console: log in there yourself, or use Access=ssh")
    if isinstance(err, paramiko.AuthenticationException):
        return "AUTH FAILED", "login refused: check User/Password"
    if isinstance(err, paramiko.BadHostKeyException):
        return "HOST KEY CHANGED", ("the system's SSH key differs from last time; if it was replaced "
                                    "or reinstalled run: fleet.py forget NAME")
    if isinstance(err, (socket.timeout, TimeoutError)):
        return "UNREACHABLE", "no answer (timeout): wrong IP, powered off, or no route"
    if isinstance(err, (ConnectionRefusedError, paramiko.ssh_exception.NoValidConnectionsError)):
        return "UNREACHABLE", "connection refused: SSH not running or wrong port"
    if isinstance(err, socket.gaierror):
        return "UNREACHABLE", "unknown host name"
    if isinstance(err, OSError):
        return "UNREACHABLE", str(err)
    return "ERROR", f"{type(err).__name__}: {err}"


# ---------- health check ----------

def flags(res):
    """Quick automatic hints from the check output; the analysis is up to the reader."""
    out = []
    res = {k: ("" if v.startswith(("(failed:", "(timed out")) else v) for k, v in res.items()}
    m = re.search(r"^read-only: (.*)$", res.get("disk", ""), re.M)
    ro = set(m.group(1).split()) if m else set()    # images like /run/initramfs/ro are always full
    for line in res.get("disk", "").splitlines()[1:]:
        m = re.search(r"(\d+)%\s+(\S+)$", line)
        if m and int(m.group(1)) >= 90 and m.group(2) not in ro:
            out.append(f"disk {m.group(2)} {m.group(1)}% full")
    for line in res.get("inodes", "").splitlines()[1:]:
        m = re.search(r"(\d+)%\s+(\S+)$", line)
        if m and int(m.group(1)) >= 90:
            out.append(f"inodes {m.group(2)} {m.group(1)}% used")
    m = re.search(r"^Mem:\s+(\d+)\s+\d+\s+\d+\s+\d+\s+\d+\s+(\d+)", res.get("memory", ""), re.M)
    if m and int(m.group(1)) and int(m.group(2)) * 100 // int(m.group(1)) < 10:
        out.append(f"memory low ({m.group(2)} MB available of {m.group(1)})")
    failed = [l.strip().lstrip("●*").split()[0] for l in res.get("failed_units", "").splitlines() if l.strip().lstrip("●*").strip()]
    failed = [u for u in failed if u]
    if failed:
        out.append(f"{len(failed)} failed unit(s): {', '.join(failed[:5])}")
    m = re.search(r"error lines this boot: (\d+)", res.get("errors_this_boot", ""))
    if m and int(m.group(1)):
        out.append(f"{m.group(1)} error log line(s) this boot")
    if re.search(r"System clock synchronized:\s*no", res.get("time", "")):
        out.append("clock not synchronized")
    return out


def _uniq(seq):
    return list(dict.fromkeys(seq))


def _smi_rows(txt):
    lines = [l for l in txt.splitlines() if l.strip()]
    if not lines or not lines[0].startswith("index"):
        return None
    head = [h.strip() for h in lines[0].split(",")]
    return [dict(zip(head, (v.strip() for v in l.split(",")))) for l in lines[1:]]


def _field(row, prefix):
    v = next((v for k, v in row.items() if k.startswith(prefix)), "")
    return int(v) if v.isdigit() else None


def hwerr_records(raw):
    """GHES record ids in a 'Hardware Error' log."""
    return _uniq(re.findall(r"\{(\d+)\}\[Hardware Error\]", raw or ""))


def gpu_flags(res, raw, s):
    out = []
    smi = res.get("gpu_smi", "")
    rows = _smi_rows(smi)
    if rows is None:
        out.append(f"nvidia-smi failed: {(smi.strip().splitlines() or ['no output'])[0][:90]}")
        rows = []
    else:
        if s["expected_gpus"] and len(rows) < s["expected_gpus"]:
            out.append(f"{len(rows)} GPU(s) in nvidia-smi, expected {s['expected_gpus']}")
        mg = re.search(r"gen\s*(\d+)", s["expected_pcie"], re.I)
        mw = re.search(r"x\s*(\d+)", s["expected_pcie"], re.I)
        for r in rows:
            i = r.get("index", "?")
            unc, cor = _field(r, "ecc.errors.uncorrected"), _field(r, "ecc.errors.corrected")
            if unc:
                out.append(f"GPU {i}: {unc} uncorrected ECC error(s)")
            if cor:
                out.append(f"GPU {i}: {cor} corrected ECC error(s) (soft)")
            gen, width = _field(r, "pcie.link.gen.current"), _field(r, "pcie.link.width.current")
            if mw and width is not None and width < int(mw.group(1)):
                out.append(f"GPU {i}: PCIe x{width}, expected x{mw.group(1)}")
            if mg and gen is not None and gen < int(mg.group(1)):
                out.append(f"GPU {i}: PCIe Gen{gen}, expected Gen{mg.group(1)} (idle GPUs slow down; recheck under load)")
    if s["expected_nvlink"] and rows:              # (no NVLink verdict when nvidia-smi itself failed)
        links = re.findall(r"Link (\d+): (.+)", res.get("gpu_nvlink", ""))
        down = [l for l in links if "inactive" in l[1].lower()]
        if not links:
            out.append("NVLink: no links reported, expected active")
        elif down:
            out.append(f"NVLink: {len(down)} of {len(links)} link(s) inactive")
    kg = res.get("kern_gpu", "")
    xids = _uniq(f"XID {x} on {bdf}" for bdf, x in re.findall(r"Xid \(PCI:([0-9A-Fa-f:.]+)\): (\d+)", kg))
    out += xids[:6]
    if re.search(r"RmFetchGspRmImages: No firmware image|RmInitAdapter failed|GSP.{0,60}(fail|error|timeout)", kg, re.I):
        out.append("GSP init failed/error (see kern_gpu)")
    fw = _uniq(re.findall(r"Direct firmware load for (\S+) failed", kg))
    if fw:
        out.append(f"driver firmware file(s) missing: {', '.join(fw[:3])}" + (" ..." if len(fw) > 3 else ""))
    drv = res.get("gpu_drivers", "")
    vers = {}
    for line in drv.splitlines():
        m = re.search(r"\b(\d{3}\.\d+(?:\.\d+)?)\b", line)
        if m and ":" in line:
            vers.setdefault(m.group(1), _uniq([]))
            vers[m.group(1)].append(line.split(":", 1)[0])
    if len(vers) > 1:
        out.append("NVIDIA driver version mismatch: " +
                   " / ".join(f"{v} ({', '.join(_uniq(src))})" for v, src in vers.items()))
    mods = _uniq(re.findall(r"(\S+): (?:disagrees about version of symbol|Unknown symbol)", res.get("modules_bad", "")))
    if mods:
        out.append(f"kernel module symbol mismatch (DKMS?): {', '.join(mods[:4])}")
    aer = [l for l in res.get("kern_pcie", "").splitlines() if re.search(r"AER.*(Uncorrect|Fatal)", l, re.I)]
    if aer:
        out.append(f"{len(aer)} PCIe AER uncorrected/fatal line(s) (see kern_pcie)")
    m = re.search(r"now (\d+) uptime ([\d.]+) first ([\d.]+)", res.get("boot_time", ""))
    if m:
        skew = float(m.group(3)) - (int(m.group(1)) - float(m.group(2)))
        if abs(skew) > 86400:
            out.append(f"clock wrong at boot: the first log entry of this boot is {abs(skew) / 86400:.0f} day(s) "
                       f"{'ahead' if skew > 0 else 'behind'}")
    n = len(hwerr_records(raw.get("hwerr")))
    if n:
        out.append(f"{n} hardware error (CPER) record(s) this boot: raw/hwerr.txt")
    p = len(hwerr_records(raw.get("hwerr_prev")))
    if p:
        out.append(f"{p} hardware error (CPER) record(s) in the previous boot: raw/hwerr_prev.txt")
    return out


# ---------- HMC behind a BMC (Redfish GET only, sent from the BMC's shell) ----------

def is_bmc(s):
    return s["type"] == "bmc" or s["profile"] == "bmc-openbmc"


def _hmc_script(ip):
    """Runs on the BMC: GET requests to the HMC only (no POST/PATCH/DELETE), each
    response after a '@@@ <path>' line; then the BMC's copy of the HMC console."""
    return f"""H=http://{ip}
g() {{ echo "@@@ $1"; curl -s -m 60 "$H$1"; echo; }}
ping -c 1 -W 2 {ip} >/dev/null 2>&1 && echo "@@@ ping ok" || echo "@@@ ping fail"
command -v curl >/dev/null || {{ echo "@@@ reach no-curl-on-bmc"; exit 0; }}
c=$(curl -s -m 10 -o /dev/null -w '%{{http_code}} %{{time_total}}' $H/redfish/v1/); echo "@@@ reach $c"
[ "${{c%% *}}" = 200 ] || exit 0
for p in Systems Managers; do
  g /redfish/v1/$p
  for m in $(curl -s -m 30 $H/redfish/v1/$p | grep -o "/redfish/v1/$p/[^\\"]*"); do
    g "$m"; g "$m/LogServices/EventLog/Entries"; g "$m/LogServices/Dump/Entries"
  done
done
g /redfish/v1/Chassis
for c in $(curl -s -m 30 $H/redfish/v1/Chassis | grep -o '/redfish/v1/Chassis/[^"]*'); do g "$c"; done
g '/redfish/v1/UpdateService/FirmwareInventory?$expand=.($levels=1)'
f=/var/log/obmc-console-HMC.log
[ -f $f ] && {{ echo "@@@ console"; tail -c 6000 $f | od -v -x; }}
"""


def _sections(text):
    parts = re.split(r"^@@@ (.*)$", text, flags=re.M)
    out = []
    for i in range(1, len(parts) - 1, 2):
        try:
            js = json.loads(parts[i + 1])
        except ValueError:
            js = None
        out.append((parts[i].strip(), js, parts[i + 1]))
    return out


def _od_x(text):
    """BusyBox `od -v -x` (no -A/-t there) back to text, escape codes removed."""
    b = bytearray()
    for line in text.splitlines():
        for w in line.split()[1:]:
            b += int(w, 16).to_bytes(2, "little")
    t = b.decode("utf-8", "replace")
    return _ANSI.sub("", t).replace("\r", "")


def _ts(v):
    try:
        return datetime.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def _kind(msg):
    return re.sub(r"\d+", "N", msg or "")[:110]


NVCPERTOOL = os.environ.get("FLEET_NVCPERTOOL", os.path.expanduser("~/nvcpertool"))


def nvcper_decode(d):
    """NVCPERTool (Pi-side, no system access) over d/*.json -> Counter{(action, signature)} or None."""
    import collections
    import glob
    import subprocess
    exe = os.path.join(NVCPERTOOL, "venv", "bin", "nvcpertool")
    rules = sorted(glob.glob(os.path.join(NVCPERTOOL, "rules", "*", "catalog_rules.json")))
    if not (os.access(exe, os.X_OK) and rules):
        return None
    rep = d.rstrip("/") + "-report.json"
    subprocess.run([exe, "dir", "--rules", rules[-1], "--report", rep, "--dir", d],
                   capture_output=True, timeout=600)
    with open(rep) as f:
        acts = json.load(f).get("Actions", [])
    return collections.Counter((a.get("action"), (a.get("signature", {}).get("error_signature") or {}).get("ip_signature"))
                               for a in acts)


def _probe_script(ip):
    """Runs on the BMC: is the HMC alive (ping) and does Redfish answer (one GET)."""
    return f"""ping -c 1 -W 2 {ip} >/dev/null 2>&1 && echo "@@@ ping ok" || echo "@@@ ping fail"
command -v curl >/dev/null || {{ echo "@@@ reach no-curl-on-bmc"; exit 0; }}
echo "@@@ reach $(curl -s -m 10 -o /dev/null -w '%{{http_code}} %{{time_total}}' http://{ip}/redfish/v1/)"
"""


def _hmc_status(text):
    """'@@@ ping ok|fail' and '@@@ reach <code> <seconds>' lines -> status dict."""
    ping = re.search(r"^@@@ ping (ok|fail)", text, re.M)
    m = re.search(r"^@@@ reach (\S+)(?: ([\d.]+))?", text, re.M)
    code = m.group(1) if m else None
    return {"ping": (ping.group(1) == "ok") if ping else None,
            "http": code if code and code not in ("000", "no-curl-on-bmc") else None,
            "ms": round(float(m.group(2)) * 1000) if m and m.group(2) else None,
            "redfish": code == "200", "error": "no curl on the BMC" if code == "no-curl-on-bmc" else None}


def save_hmc_status(name, ip, via, **st):
    """reports/<name>/hmc_status.json: the HMC indicator on /fleet."""
    d = _dirname(name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "hmc_status.json"), "w") as f:
        json.dump(dict(st, ip=ip, via=via, checked=time.time()), f)


def cmd_hmc_probe(name):
    """Through the BMC row NAME: ping the HMC and GET /redfish/v1/ once. Nothing else."""
    s = find(load(), name)
    if not (is_bmc(s) and s["hmc"]):
        die(f"{name} is not a BMC row with an HMC (Type bmc or Profile bmc-openbmc, HMC IP not none)")
    progress(name, reset=True, kind="hmc-probe", stages=["connect", "hmc"], stage="connect", state="active",
             step=f"ssh {s['user']}@{s['ip']}:{s['port']}", started=time.time())
    try:
        sess = open_session(s)
    except Exception as e:                           # noqa: BLE001
        st, why = classify(e)
        save_hmc_status(name, s["hmc"], "probe", bmc=st, ping=None, http=None, redfish=False, error=why)
        progress(name, state="error", result=st, step=why[:90], finished=time.time())
        die(f"{name}: BMC {st}: {why}")
    progress(name, stage="hmc", step=f"ping + GET http://{s['hmc']}/redfish/v1/")
    try:
        _, text = sess.run(_probe_script(s["hmc"]), 40)
    finally:
        sess.close()
    st = _hmc_status(text)
    save_hmc_status(name, s["hmc"], "probe", bmc="OK", **st)
    ok = st["redfish"]
    progress(name, state="done" if ok else "error", finished=time.time(),
             result="HMC OK" if ok else ("HMC no Redfish" if st["ping"] else "HMC down"),
             step=f"ping {'ok' if st['ping'] else 'no answer'}, Redfish {st['http'] or 'no answer'}"
                  + (f" in {st['ms']} ms" if st["ms"] is not None else ""))
    print(json.dumps(st))


def hmc_survey(sess, s, rep):
    """Read the HMC's state and logs through the BMC: health, firmware, event logs, dumps,
    console. Flags only events that are new since the previous check (first check: 7 days)."""
    import collections
    rc, text = sess.run(_hmc_script(s["hmc"]), max(s["timeout"], 300))
    rep["raw"]["hmc_redfish"] = text.rstrip()
    secs = _sections(text)
    st = _hmc_status(text)
    save_hmc_status(s["name"], s["hmc"], "check", bmc="OK", **st)
    reach = st["http"] or ("no-curl-on-bmc" if st["error"] else None)
    out, fl = [f"HMC {s['hmc']} (from the BMC): ping {'ok' if st['ping'] else 'no answer'}, "
               f"Redfish HTTP {reach}" + (f" in {st['ms']} ms" if st["ms"] is not None else "")], []
    if reach != "200":
        why = "no curl on the BMC" if reach == "no-curl-on-bmc" else f"HTTP {reach or 'no answer'}"
        rep["flags"].append(f"HMC {s['hmc']} not reachable from the BMC ({why})")
        rep["results"]["hmc"] = "\n".join(out)
        rep["hmc"] = {"ip": s["hmc"], "reachable": False}
        return
    now, entries, dumps = None, [], []
    health = []
    fw = []
    for path, js, _ in secs:
        if not isinstance(js, dict):
            continue
        if path.endswith("/EventLog/Entries"):
            for e in js.get("Members", []):
                entries.append(dict(e, _log=path.split("/LogServices")[0].rsplit("/", 1)[-1]))
        elif path.endswith("/Dump/Entries"):
            dumps += [(path.split("/LogServices")[0].rsplit("/", 1)[-1], e) for e in js.get("Members", [])]
        elif path.startswith("/redfish/v1/UpdateService/FirmwareInventory"):
            fw = [(m.get("Id"), m.get("Version")) for m in js.get("Members", []) if m.get("Id")]
        elif "Status" in js and path.count("/") == 4:          # /redfish/v1/<Coll>/<member>
            st = js.get("Status") or {}
            health.append((path.split("/")[3], js.get("Id"), st.get("State"), st.get("Health"), st.get("HealthRollup")))
            if js.get("DateTime") and not now:
                now = _ts(js["DateTime"])
    # "New" is decided by event Id (it only grows), not by time: the HMC clock can jump
    # (entries from 2011 next to one from 2026 were seen). First check: the last 7 days
    # on the HMC's own clock (Manager DateTime), else the last 100 entries.
    def eid(e):
        return int(e["Id"]) if str(e.get("Id", "")).isdigit() else -1
    entries = [e for e in entries if _ts(e.get("Created"))]
    entries.sort(key=lambda e: (e["_log"], eid(e)))
    try:
        with open(os.path.join(_dirname(s["name"]), "latest.json")) as f:
            prev = json.load(f).get("hmc")
        prev = prev.get("max_id") if isinstance(prev, dict) else None
    except (OSError, ValueError):
        prev = None
    max_id = {}
    for e in entries:
        max_id[e["_log"]] = max(max_id.get(e["_log"], -1), eid(e))
    if isinstance(prev, dict) and all(max_id.get(k, -1) >= v for k, v in prev.items()):
        new = [e for e in entries if eid(e) > prev.get(e["_log"], -1)]
        since = f"the last check (event Id > {prev})"
    elif now:
        cut = now - datetime.timedelta(days=7)
        new = [e for e in entries if _ts(e["Created"]) > cut]
        since = f"{cut:%Y-%m-%d %H:%M} (7 days, HMC clock)"
    else:
        new = entries[-100:]
        since = "the last 100 entries (HMC clock unknown)"
    rep["hmc"] = {"ip": s["hmc"], "reachable": True, "now": now.isoformat() if now else None,
                  "max_id": max_id, "since": since, "events": len(entries), "new_events": len(new)}

    bad = [h for h in health if (h[3] or "OK") != "OK" or (h[4] or "OK") != "OK"]
    out.append(f"HMC clock {now}; {len(health)} resources read, {len(bad)} not OK")
    for coll, i, state, h, roll in bad:
        out.append(f"  {coll}/{i}: State {state}, Health {h}, Rollup {roll}")
    if bad:
        fl.append("HMC: not OK: " + ", ".join(f"{i} {h or roll}" for _, i, _, h, roll in bad[:6])
                  + (" ..." if len(bad) > 6 else ""))
    if fw:
        out.append(f"firmware ({len(fw)}): " + ", ".join(f"{i} {v}" for i, v in fw))
    sev = collections.Counter(e.get("Severity") for e in entries)
    out.append(f"event logs: {len(entries)} entries {dict(sev)}; {len(new)} new since {since}")
    groups = collections.OrderedDict()
    for e in entries:
        groups.setdefault((e.get("Severity"), _kind(e.get("Message"))), []).append(e)
    out.append("  all entries by kind (count, first .. last, severity, message):")
    for (sv, k), es in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:25]:
        out.append(f"  {len(es):5} {es[0]['Created'][:19]} .. {es[-1]['Created'][:19]} {sv:8} {k}")
    newbad = [e for e in new if e.get("Severity") in ("Critical", "Warning")]
    if newbad:
        out.append(f"  new Critical/Warning ({len(newbad)}), newest last:")
        for e in newbad[-30:]:
            out.append(f"  {e['Created'][:19]} {e.get('Severity'):8} {e.get('Message', '')[:170]}")
        top = collections.Counter(_kind(e.get("Message")) for e in newbad).most_common(3)
        fl.append(f"HMC: {len(newbad)} new Critical/Warning event(s): " + "; ".join(f"{k[:70]} x{n}" for k, n in top))
    msgs = " ".join(e.get("Message", "") for e in new)
    for gpu, n in collections.Counter(re.findall(r"PWR_FAIL_(GPU\d+)_THERM_OVERT", msgs)).items():
        fl.append(f"HMC: {gpu} thermal over-temperature power fail x{n}")
    for dev, n in collections.Counter(re.findall(r"'MCTP Ping' failed while communicating to device '([^']+)'", msgs)).items():
        if n >= 3:
            fl.append(f"HMC: {dev} not answering MCTP ping x{n}")
    if dumps:
        out.append(f"dumps on the HMC ({len(dumps)}): " + ", ".join(f"{w}/{e.get('Id')} {str(e.get('Created'))[:19]} "
                                                                   f"{e.get('AdditionalDataSizeBytes')} B" for w, e in dumps))

    cper = [e for e in entries if "CPER" in e or e.get("DiagnosticDataType") == "CPER"]
    cnew = [e for e in cper if e in new]
    rep["hmc"].update(cper=len(cper), cper_new=len(cnew))
    sigs = collections.Counter(((e.get("CPER") or {}).get("Oem", {}).get("Nvidia", {}).get("NvidiaEvent", {})
                                .get("EventHeader", {}).get("Signature") or "?") for e in cnew)
    out.append(f"CPER records: {len(cper)} in the logs, {len(cnew)} new" + (f": {dict(sigs)}" if sigs else ""))
    if cnew:
        d = os.path.join(_dirname(s["name"]), "cper_hmc")
        os.makedirs(d, exist_ok=True)
        for f in os.listdir(d):
            if f.startswith("rf_") and f.endswith(".json"):
                os.remove(os.path.join(d, f))
        for e in cnew:
            with open(os.path.join(d, f"rf_{e.get('Id')}.json"), "w") as f:
                json.dump({k: v for k, v in e.items() if k != "_log"}, f)
        try:
            dec = nvcper_decode(d)
        except Exception as e:                       # noqa: BLE001
            dec, out[-1] = None, out[-1] + f" (decode failed: {type(e).__name__})"
        if dec is None:
            out.append(f"  saved to cper_hmc/ (NVCPERTool not installed: not decoded)")
            rep["hmc"]["cper_text"] = f"HMC {len(cnew)} new, not decoded"
        else:
            out.append("  NVCPERTool: " + ", ".join(f"{n} {a} {sg}" for (a, sg), n in dec.most_common()))
            by_act = collections.Counter()
            for (a, _), n in dec.items():
                by_act[a] += n
            rep["hmc"]["cper_text"] = f"HMC {len(cnew)} new: " + ", ".join(f"{n} {a}" for a, n in by_act.most_common())
            hw = [(a, sg, n) for (a, sg), n in dec.items() if str(a).startswith("HW-")]
            for a, sg, n in hw:
                fl.append(f"HMC CPER: {n} x {a} {sg} (hardware suspect)")
            x = sum(n for (a, _), n in dec.items() if a == "SW-X")
            if x:
                fl.append(f"HMC CPER: {x} record(s) with no NVIDIA rule (SW-X): send cper_hmc/ to NVIDIA")
            dpc = sum(n for (_, sg), n in dec.items() if sg and "DPC" in sg)
            if dpc:
                fl.append(f"HMC CPER: {dpc} PCIe link(s) cut off by DPC (see cper_hmc/)")
    con = next((raw for p, _, raw in secs if p == "console"), None)
    if con:
        lines = [l for l in _od_x(con).splitlines() if l.strip()]
        out += ["HMC console (BMC's copy), last lines:"] + ["  " + l[:200] for l in lines[-25:]]
    rep["results"]["hmc"] = "\n".join(out)
    rep["flags"] += fl


def progress(name, reset=False, **kw):
    """Live state of the work on one system, read by the /fleet page every second:
    ~/fleet/.progress/<name>.json = {kind, stages, stage, state, step, i, n, started,
    finished, result, pid}. Never holds passwords. Best effort: errors are ignored."""
    try:
        os.makedirs(PROGRESS, exist_ok=True)
        path = os.path.join(PROGRESS, re.sub(r"[^A-Za-z0-9_.-]", "_", name) + ".json")
        d = {}
        if not reset:
            try:
                with open(path) as f:
                    d = json.load(f)
            except (OSError, ValueError):
                pass
        d.update(kw, name=name, pid=os.getpid(), updated=time.time())
        tmp = f"{path}.{threading.get_ident()}.tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, path)
    except OSError:
        pass


def _stages(s):
    return ["connect", "checks"] + (["hmc"] if is_bmc(s) and s["hmc"] else []) + ["save"]


def check_one(s, stamp):
    rep = {k: s[k] for k in ("name", "ip", "port", "user", "access", "profile", "type", "server", "rack", "stage",
                             "expected_gpus", "expected_pcie", "expected_nvlink", "expected_fw", "problem")}
    rep.update(time=stamp, results={}, raw={}, flags=[], notes=[], hmc_ip=s["hmc"] if is_bmc(s) else "")
    checks, raws = list(CHECKS), []
    if s["profile"] == "gpu-vera":
        checks += GPU_CHECKS
        raws = GPU_RAW
    t0 = time.monotonic()
    nm, total = s["name"], len(checks + raws)
    progress(nm, state="active", stage="connect", started=time.time(), i=0, n=total,
             step=f"{'console' if s['access'] == 'console' else 'ssh'} {s['user']}@{s['ip']}:{s['port']}")
    try:
        sess = open_session(s)
    except Exception as e:                           # noqa: BLE001 - reported per system
        rep["status"], rep["error"] = classify(e)
        if is_bmc(s) and s["hmc"]:
            save_hmc_status(nm, s["hmc"], "check", bmc=rep["status"], ping=None, http=None, redfish=False,
                            error="BMC not reachable: HMC state unknown")
        progress(nm, state="error", result=rep["status"], step=rep["error"][:90], finished=time.time())
        return rep
    try:
        for n, (key, cmd) in enumerate(checks + raws):
            progress(nm, stage="checks", step=key, i=n)
            try:
                rc, out = sess.run(cmd, s["timeout"])
            except Exception as e:                   # noqa: BLE001
                rc, out = -1, f"(failed: {type(e).__name__}: {e})"
            (rep["raw"] if (key, cmd) in raws else rep["results"])[key] = out.rstrip()
            if sess.console and rc in (124, -1) and n < 2:
                # A console that cannot even run hostname/uptime is hung (e.g. its root disk
                # stopped answering): stop here instead of waiting Timeout for every check.
                rep["status"] = "HUNG"
                rep["error"] = (f"the console shell did not finish '{key}' ({out.strip().splitlines()[-1][:80]}): "
                                "the host is hung or its disk stopped answering; the BMC's copy of the "
                                "host console log shows why")
                progress(nm, state="error", result="HUNG", step=f"{key} did not finish", finished=time.time())
                return rep
        rep["flags"] = flags(rep["results"])
        if s["profile"] == "gpu-vera":
            rep["flags"] += gpu_flags(rep["results"], rep["raw"], s)
        rep["cper_records"] = len(hwerr_records(rep["raw"].get("hwerr")))
        progress(nm, i=total)
        if is_bmc(s) and s["hmc"]:
            progress(nm, stage="hmc", step=f"Redfish GET via the BMC -> {s['hmc']}")
            try:
                hmc_survey(sess, s, rep)
            except Exception as e:                   # noqa: BLE001 - reported, the BMC part stands
                rep["results"]["hmc"] = f"(HMC survey failed: {type(e).__name__}: {e})"
                rep["flags"].append(f"HMC {s['hmc']}: survey failed ({type(e).__name__})")
        rep["status"] = "WARN" if rep["flags"] else "OK"
        if not any(v.strip() for v in rep["results"].values()):
            rep["status"] = "NO OUTPUT"
            rep["error"] = ("logged in, but every command came back empty: if this port is a serial "
                            "console (e.g. a host behind its BMC), set Access to console"
                            if not sess.console else "the console answered nothing: check that the host is up")
    finally:
        sess.close()
    rep["seconds"] = round(time.monotonic() - t0)
    return rep


def save_raw(name, key, text, t):
    d = os.path.join(_dirname(name), "raw")
    os.makedirs(d, exist_ok=True)
    for fn in (f"{key}-{t}.txt", f"{key}.txt"):
        with open(os.path.join(d, fn), "w") as f:
            f.write(text + ("\n" if text else ""))
    return os.path.join(d, f"{key}.txt")


def save_report(rep):
    d = _dirname(rep["name"])
    os.makedirs(d, exist_ok=True)
    t = rep["time"].replace(":", "").replace("-", "").replace(" ", "-")
    lines = [f"# {rep['name']}  {rep['ip']}:{rep['port']}  user {rep['user']}  {rep['time']}",
             f"# access {rep['access']}, profile {rep['profile']}"
             + "".join(f", {k} {rep[k]}" for k in ("type", "server", "rack", "stage", "hmc_ip") if rep.get(k)),
             f"# status: {rep['status']}" + (f" ({rep['seconds']} s)" if "seconds" in rep else "")]
    if rep.get("problem"):
        lines.append(f"# reported problem: {rep['problem']}")
    if rep.get("error"):
        lines.append(f"# error: {rep['error']}")
    lines += [f"# note: {n}" for n in rep.get("notes", [])]
    lines += [f"# flag: {f}" for f in rep["flags"]]
    for key, out in rep["results"].items():
        lines += ["", f"===== {key} =====", out]
    files = {}
    for key, text in rep["raw"].items():
        if not text:
            continue
        save_raw(rep["name"], key, text, t)
        n = text.count("\n") + 1
        files[key] = {"file": f"raw/{key}.txt", "lines": n}
        lines += ["", f"===== {key} (raw) =====", f"{n} line(s) saved in full to raw/{key}.txt"]
    rep = dict(rep, raw=files)
    path = os.path.join(d, f"{t}.txt")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    with open(os.path.join(d, "latest.json"), "w") as f:
        json.dump(rep, f, indent=1)
    try:
        link = os.path.join(d, "latest.txt")
        if os.path.lexists(link):
            os.remove(link)
        os.symlink(os.path.basename(path), link)
    except OSError:
        pass
    return path


def cmd_check(names, workers):
    rows = load()
    targets = [find(rows, n) for n in names] if names else rows
    if not targets:
        die("no systems in the list yet: add them on the /fleet page")
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    slow = [s["name"] for s in targets if s["access"] == "console"]
    if slow:
        print(f"console systems ({', '.join(slow)}) are typed into slowly: a few minutes each", flush=True)
    for s in targets:
        progress(s["name"], reset=True, kind="check", stages=_stages(s), stage=None, state="queued",
                 step="waiting for a free worker", i=0, n=0)

    def work(s):
        rep = check_one(s, stamp)
        if rep.get("status") not in ("AUTH FAILED", "UNREACHABLE", "HOST KEY CHANGED", "ERROR", "CONSOLE LOGIN", "HUNG"):
            progress(s["name"], stage="save", step="writing the report")
        path = save_report(rep)
        ok = rep["status"] in ("OK", "WARN")
        progress(s["name"], state="done" if ok else "error", result=rep["status"], finished=time.time(),
                 step=(f"{len(rep['flags'])} flag(s)" if ok else (rep.get("error") or "")[:90]))
        return rep, path

    updates, reports = {}, []
    with concurrent.futures.ThreadPoolExecutor(workers) as ex:
        for rep, path in ex.map(work, targets):
            reports.append(rep)
            note = rep.get("error") or "; ".join(rep["flags"]) or "no problems found by the checks"
            print(f"{rep['status']:<16} {rep['name']:<20} {rep['ip']:<16} {note}")
            print(f"{'':16} report: {path}", flush=True)
            n = rep.get("cper_records", 0)
            cper = [f"host {n} record(s), not decoded yet"] if n else []
            h = rep.get("hmc") if isinstance(rep.get("hmc"), dict) else {}
            if h.get("cper_text"):
                cper.append(h["cper_text"])
            elif h.get("cper"):
                cper.append(f"HMC {h['cper']} in log, 0 new")
            updates[rep["name"]] = {"Status": rep["status"], "Last check": stamp,
                                    "Flags": rep.get("error") or "; ".join(rep["flags"]), "CPER": "; ".join(cper)}
    write_back(updates)
    bad = sum(r["status"] != "OK" for r in reports)
    print(f"\n{len(reports)} checked, {bad} need a look. Results saved in the list ({STORE})")


def _one_command(name, cmd, sudo, kind="run"):
    """-> (system, exit code, output) for run / collect."""
    rows = load()
    s = find(rows, name)
    pw = None
    if sudo and s["access"] == "console":
        print("fleet: console session: no sudo, the command runs as the console's user", file=sys.stderr)
    elif sudo and s["user"] != "root":
        if not s["password"]:
            die("--sudo needs the Password column filled in")
        pw = s["password"]
    label = ("sudo " if sudo else "") + " ".join(cmd.split())[:80]
    progress(s["name"], reset=True, kind=kind, stages=["connect", kind], stage="connect", state="active",
             step=label, started=time.time())
    try:
        sess = open_session(s)
    except Exception as e:                           # noqa: BLE001
        st, why = classify(e)
        progress(s["name"], state="error", result=st, step=why[:90], finished=time.time())
        die(f"{s['name']}: {st}: {why}")
    progress(s["name"], stage=kind)
    try:
        rc, out = sess.run(cmd, max(s["timeout"], 120), pw)
    except Exception as e:                           # noqa: BLE001
        progress(s["name"], state="error", result="ERROR", step=f"{type(e).__name__}: {e}"[:90], finished=time.time())
        raise
    finally:
        sess.close()
    progress(s["name"], state="done" if rc == 0 else "error", result=f"exit {rc}", finished=time.time())
    return s, rc, out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("export"); p.add_argument("file")
    sub.add_parser("edit")
    sub.add_parser("list")
    p = sub.add_parser("check"); p.add_argument("names", nargs="*")
    p.add_argument("--workers", type=int, default=WORKERS, help=f"systems checked at once (default {WORKERS})")
    p = sub.add_parser("run"); p.add_argument("name"); p.add_argument("--sudo", action="store_true")
    p.add_argument("command", nargs=argparse.REMAINDER)
    p = sub.add_parser("collect"); p.add_argument("name"); p.add_argument("key")
    p.add_argument("--sudo", action="store_true"); p.add_argument("command", nargs=argparse.REMAINDER)
    p = sub.add_parser("note"); p.add_argument("name"); p.add_argument("text")
    p = sub.add_parser("forget"); p.add_argument("name")
    p = sub.add_parser("hmc-probe"); p.add_argument("name")
    a = ap.parse_args()

    if a.cmd == "export":
        export_xlsx(a.file)
        print(f"exported (no passwords): {a.file}")
    elif a.cmd == "edit":
        cmd_edit()
    elif a.cmd == "list":
        rows = load()
        print(f"{STORE}, {len(rows)} system(s)")
        print(f"{'NAME':<18} {'ADDRESS':<22} {'USER':<9} {'PW':<3} {'ACCESS/PROFILE':<20} {'SERVER':<10} "
              f"{'STATUS':<14} {'LAST CHECK':<20} PROBLEM / SUMMARY")
        for s in rows:
            pw = "yes" if s["password"] else "-"
            txt = s["problem"] + (f" | {s['summary']}" if s["summary"] else "")
            print(f"{s['name']:<18} {s['ip'] + ':' + str(s['port']):<22} {s['user']:<9} {pw:<3} "
                  f"{s['access'] + '/' + s['profile']:<20} {s['server'] or '-':<10} {s['status']:<14} {s['last']:<20} {txt}")
    elif a.cmd == "check":
        cmd_check(a.names, max(1, a.workers))
    elif a.cmd in ("run", "collect"):
        cmd = a.command[1:] if a.command[:1] == ["--"] else a.command
        if not cmd:
            die(f"usage: fleet.py {a.cmd} NAME {'KEY ' if a.cmd == 'collect' else ''}[--sudo] -- COMMAND")
        if a.cmd == "collect" and not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", a.key):
            die("KEY: letters, digits, - and _ only")
        s, rc, out = _one_command(a.name, " ".join(cmd), a.sudo, a.cmd)
        if a.cmd == "run":
            sys.stdout.write(out if out.endswith("\n") or not out else out + "\n")
        else:
            t = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            path = save_raw(s["name"], a.key, out.rstrip("\n"), t)
            print(f"{out.count(chr(10)) + (1 if out and not out.endswith(chr(10)) else 0)} line(s) saved to {path}")
        print(f"[{s['name']} exit {rc}]")
        sys.exit(0 if rc == 0 else 1)
    elif a.cmd == "note":
        rows = load()
        write_back({find(rows, a.name)["name"]: {"Summary": a.text}})
        print("summary written")
    elif a.cmd == "hmc-probe":
        cmd_hmc_probe(a.name)
    elif a.cmd == "forget":
        rows = load()
        s = find(rows, a.name)
        if os.path.exists(KNOWN_HOSTS):
            import paramiko
            hk = paramiko.HostKeys(KNOWN_HOSTS)
            for key in (s["ip"], f"[{s['ip']}]:{s['port']}"):
                if key in hk:
                    del hk[key]
            hk.save(KNOWN_HOSTS)
        print(f"host key for {s['name']} forgotten; the next connection remembers the new one")


if __name__ == "__main__":
    main()
