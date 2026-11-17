"""System clone: copy the Pi's whole system disk (the SD card it booted from) to
another card in a USB card reader, so the copy can replace the original.

How (the rpi-clone way, safe on a running system):
  1. the target's old contents are wiped and the source's partition table is
     written to it (same disk id, so the same PARTUUIDs; the last partition
     grows or shrinks to the target's size),
  2. each file system is made fresh with the same label and UUID
     (system-boot / writable, which cmdline.txt and fstab boot by),
  3. every file is copied with rsync (root: owners, modes, links, ACLs, xattrs;
     /proc /sys /dev /run /tmp /mnt /media are kept as empty directories),
  4. the copy is checked (e2fsck -n, fsck.vfat -n) and unmounted.

The copy carries the same labels and UUIDs as the original, so with both cards
in, Linux may mount the wrong one: take the copy out before rebooting.

Only one clone at a time; it runs in a thread and the page polls status()."""

import hashlib
import json
import os
import re
import subprocess
import threading
import time

_WORK = "/run/pi-tester/clone"
_EXCLUDE = ["/proc/*", "/sys/*", "/dev/*", "/run/*", "/tmp/*", "/mnt/*", "/media/*", "/lost+found"]
_lock = threading.Lock()
_state = {"running": False, "dev": None, "step": "", "pct": 0, "log": [], "error": None, "done": False,
          "started": None, "finished": None, "cancel": False}
_proc = None


def _run(*cmd, timeout=120, check=True, inp=None):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=inp)
    if check and r.returncode:
        raise RuntimeError(f"{os.path.basename(cmd[0])} failed: {(r.stderr or r.stdout).strip()[-300:]}")
    return r.stdout


def _lsblk(*devs):
    out = _run("lsblk", "-J", "-b", "-o", "NAME,PATH,PKNAME,SIZE,TYPE,TRAN,RM,RO,MODEL,SERIAL,FSTYPE,LABEL,UUID,MOUNTPOINTS",
               *devs, timeout=15)
    return json.loads(out)["blockdevices"]


def _disk_of(mountpoint):
    """Whole disk under a mount point, e.g. / -> /dev/mmcblk0."""
    src = _run("findmnt", "-n", "-o", "SOURCE", mountpoint, timeout=10).strip()
    pk = _run("lsblk", "-n", "-o", "PKNAME", src, timeout=10).strip()
    return "/dev/" + pk if pk else src


def _port(name):
    """USB port of a disk from sysfs, e.g. '2-1' (on the Pi) or '4-2.4.1' (through a hub)."""
    ports = re.findall(r"/(\d+-[\d.]+)(?=/)", os.path.realpath(f"/sys/block/{name}"))
    return ports[-1] if ports else ""


def _fingerprint(dev):
    """Hash of the card's first 1 MiB: changes when another card goes into the same reader."""
    try:
        with open(dev, "rb") as f:
            return hashlib.sha1(f.read(1 << 20)).hexdigest()[:12]
    except OSError:
        return ""


def _ident(d, port):
    return "|".join([d.get("serial") or "", port, str(d["size"]), _fingerprint(d["path"]) if d["size"] else ""])


def _part(disk, n):
    return f"{disk}p{n}" if disk[-1].isdigit() else f"{disk}{n}"


def _used():
    """Bytes the copy needs on each source file system."""
    out = {}
    for mp in ("/", "/boot/firmware"):
        st = os.statvfs(mp)
        out[mp] = (st.f_blocks - st.f_bfree) * st.f_frsize
    return out


def source():
    disk = _disk_of("/")
    d = _lsblk(disk)[0]
    used = _used()
    return {"dev": disk, "size": d["size"], "model": d.get("model"),
            "parts": [{"name": p["name"], "size": p["size"], "fstype": p.get("fstype"), "label": p.get("label")}
                      for p in d.get("children") or []],
            "used": used["/"] + used["/boot/firmware"]}


def targets():
    """Disks a clone may go to: removable or USB, not the system disk, not read-only."""
    sysdisk = {_disk_of("/"), _disk_of("/boot/firmware")}
    need = _need()
    out = []
    for d in _lsblk():
        if d["type"] != "disk" or d["path"] in sysdisk or d["name"].startswith(("loop", "zram", "ram")):
            continue
        if not (d.get("rm") or d.get("tran") == "usb"):
            continue
        parts = d.get("children") or []
        mounts = [m for p in [d] + parts for m in (p.get("mountpoints") or []) if m]
        why = None
        if not d["size"]:
            why = "no card in this reader"
        elif d.get("ro"):
            why = "card is write-protected"
        elif d["size"] < need:
            why = f"too small: needs at least {need / 1e9:.1f} GB"
        port = _port(d["name"])
        out.append({"dev": d["path"], "name": d["name"], "size": d["size"], "model": (d.get("model") or "").strip(),
                    "tran": d.get("tran"), "serial": d.get("serial"), "port": port, "hub": "." in port,
                    "id": _ident(d, port), "mounted": mounts, "ok": why is None, "why": why,
                    "parts": [{"name": p["name"], "size": p["size"], "fstype": p.get("fstype"),
                               "label": p.get("label")} for p in parts]})
    return out


def _need():
    """Smallest target: the boot partitions as they are + the used root space + 10 % + 256 MiB."""
    lay = _layout(_disk_of("/"))
    first = lay["parts"][-1]["start"] * 512
    return first + int(_used()["/"] * 1.10) + (256 << 20)


def _layout(disk):
    d = json.loads(_run("sfdisk", "-J", disk, timeout=15))["partitiontable"]
    return {"label": d["label"], "id": d.get("id"), "parts": d["partitions"]}


def status():
    with _lock:
        return {k: v for k, v in _state.items() if k != "cancel"}


def _log(msg, step=None, pct=None):
    with _lock:
        _state["log"].append(f"{time.strftime('%H:%M:%S')} {msg}")
        _state["log"] = _state["log"][-200:]
        if step is not None:
            _state["step"] = step
        if pct is not None:
            _state["pct"] = pct
    print(f"clone: {msg}", flush=True)


def start(dev, ident, who="?"):
    with _lock:
        if _state["running"]:
            raise ValueError("a clone is already running")
    t = {x["dev"]: x for x in targets()}.get(dev)
    if not t:
        raise ValueError(f"{dev} is not a card that can be cloned to (system disk, not removable, or gone)")
    if not t["ok"]:
        raise ValueError(f"{dev}: {t['why']}")
    if t["id"] != ident:
        raise ValueError(f"the card at {dev} changed since the list was shown (other card, reader or port): "
                         "check which card is where and confirm again")
    with _lock:
        _state.update(running=True, dev=dev, step="starting", pct=0, log=[], error=None, done=False,
                      started=time.time(), finished=None, cancel=False)
    _log(f"clone of {_disk_of('/')} to {dev} ({t['model']}, {t['size'] / 1e9:.1f} GB) started by {who}")
    threading.Thread(target=_work, args=(dev,), daemon=True, name="clone").start()


def cancel():
    with _lock:
        if not _state["running"]:
            return False
        _state["cancel"] = True
    p = _proc
    if p and p.poll() is None:
        p.terminate()
    return True


def _check_cancel():
    if _state["cancel"]:
        raise RuntimeError("cancelled: the copy is incomplete and will not boot; run the clone again")


def _umount_all(dev):
    for p in reversed(_lsblk(dev)[0].get("children") or []):
        for m in p.get("mountpoints") or []:
            if m:
                _run("umount", "-l", m, check=False)


def _rsync(args, total, pct0, pct1, step):
    """rsync with --info=progress2; bytes copied -> pct0..pct1."""
    global _proc
    _proc = subprocess.Popen(["rsync", *args, "--info=progress2", "--no-inc-recursive"], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, bufsize=1)
    buf, last = "", 0
    while True:
        ch = _proc.stdout.read(1)
        if not ch:
            break
        if ch in "\r\n":
            m = re.match(r"\s*([\d,]+)\s+(\d+)%\s+(\S+)", buf)
            if m and time.time() - last > 0.5:
                done = int(m.group(1).replace(",", ""))
                frac = min(1.0, done / total) if total else int(m.group(2)) / 100
                with _lock:
                    _state["pct"] = round(pct0 + (pct1 - pct0) * frac, 1)
                    _state["step"] = f"{step}: {done / 1e9:.2f} of ~{total / 1e9:.2f} GB, {m.group(3)}"
                last = time.time()
            buf = ""
        else:
            buf += ch
    err = _proc.stderr.read()
    rc = _proc.wait()
    _proc = None
    _check_cancel()
    # 24 = files vanished while copying (normal on a live system)
    if rc not in (0, 24):
        raise RuntimeError(f"rsync exit {rc}: {err.strip()[-400:]}")
    if rc == 24:
        _log("some files changed or vanished during the copy (normal on a running system)")


def _work(dev):
    root_mp, boot_mp = f"{_WORK}/root", f"{_WORK}/root/boot/firmware"
    mounted = []
    try:
        src = _disk_of("/")
        lay = _layout(src)
        used = _used()
        srcp = {p["node"]: p for p in lay["parts"]}
        info = {p["path"]: p for p in _lsblk(src)[0]["children"]}
        boot_src = _run("findmnt", "-n", "-o", "SOURCE", "/boot/firmware", timeout=10).strip()
        root_src = _run("findmnt", "-n", "-o", "SOURCE", "/", timeout=10).strip()
        if [p["node"] for p in lay["parts"]] != [boot_src, root_src]:
            raise RuntimeError(f"unexpected layout on {src}: {list(srcp)} (expected boot then root)")

        _log("unmounting and wiping the target", "wipe", 1)
        _umount_all(dev)
        for p in _lsblk(dev)[0].get("children") or []:
            _run("wipefs", "-a", p["path"], check=False)
        _run("wipefs", "-a", dev)
        _run("dd", "if=/dev/zero", f"of={dev}", "bs=1M", "count=16", "conv=fsync", "status=none", timeout=60)
        _check_cancel()

        _log("writing the partition table (same disk id as the original)", "partition", 3)
        lines = [f"label: {lay['label']}"] + ([f"label-id: {lay['id']}"] if lay.get("id") else []) + ["unit: sectors"]
        for i, p in enumerate(lay["parts"]):
            last = i == len(lay["parts"]) - 1
            f = [f"start={p['start']}"] + ([] if last else [f"size={p['size']}"]) + [f"type={p['type']}"]
            if p.get("bootable"):
                f.append("bootable")
            lines.append(", ".join(f))
        _run("sfdisk", "--wipe", "always", dev, inp="\n".join(lines) + "\n", timeout=60)
        _run("partprobe", dev, check=False)
        _run("udevadm", "settle", check=False)
        time.sleep(1)
        tb, tr = _part(dev, 1), _part(dev, 2)
        for x in (tb, tr):
            for _ in range(20):
                if os.path.exists(x):
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError(f"{x} did not appear after partitioning")

        b, r = info[boot_src], info[root_src]
        _log(f"making FAT32 '{b['label']}' (id {b['uuid']}) and ext4 '{r['label']}' (UUID {r['uuid']})", "format", 5)
        _run("mkfs.vfat", "-F", "32", "-n", b["label"] or "system-boot", "-i", (b["uuid"] or "").replace("-", ""), tb,
             timeout=120)
        _run("mkfs.ext4", "-q", "-F", "-L", r["label"] or "writable", "-U", r["uuid"], tr, timeout=600)
        _check_cancel()

        os.makedirs(root_mp, exist_ok=True)
        _run("mount", tr, root_mp)
        mounted.append(root_mp)
        _log("copying the root file system", "copy root", 8)
        _rsync(["-aHAXx", "--numeric-ids", "--delete", *[f"--exclude={e}" for e in _EXCLUDE], "/", root_mp + "/"],
               used["/"], 8, 92, "copying root")

        os.makedirs(boot_mp, exist_ok=True)
        _run("mount", tb, boot_mp)
        mounted.append(boot_mp)
        _log("copying the boot partition", "copy boot", 92)
        _rsync(["-rtx", "--modify-window=2", "--delete", "/boot/firmware/", boot_mp + "/"],
               used["/boot/firmware"], 92, 96, "copying boot")

        _log("writing everything to the card", "sync", 96)
        _run("sync", timeout=1200)
        for m in reversed(mounted):
            _run("umount", m, timeout=300)
        mounted.clear()

        _log("checking the copy (read-only fsck)", "verify", 98)
        e = subprocess.run(["e2fsck", "-f", "-n", tr], capture_output=True, text=True, timeout=1200)
        if e.returncode not in (0,):
            raise RuntimeError(f"e2fsck found problems on the copy (exit {e.returncode}): {e.stdout.strip()[-300:]}")
        v = subprocess.run(["fsck.vfat", "-n", tb], capture_output=True, text=True, timeout=300)
        if v.returncode not in (0,):
            raise RuntimeError(f"fsck.vfat found problems on the copy: {v.stdout.strip()[-300:]}")
        with _lock:
            _state.update(done=True, pct=100, step="done")
        _log("done: take the copy out before rebooting (both cards carry the same labels and UUIDs)")
    except Exception as e:                                # noqa: BLE001 - shown on the page
        with _lock:
            _state["error"] = str(e)
        _log(f"FAILED: {e}")
    finally:
        for m in reversed(mounted):
            _run("umount", "-l", m, check=False)
        with _lock:
            _state.update(running=False, finished=time.time())
