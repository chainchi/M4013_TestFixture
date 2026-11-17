"""/fleet: the list of systems (DUTs) for fleet debugging, edited on the page
(add / change / delete, no file upload), the health check, and the reports.
The work is done by ~TERMINAL_USER/fleet/fleet.py (see tools/fleet/CLAUDE.md);
edits go through `fleet.py edit` so validation and locking live in one place.

The list holds SSH passwords, so the page opens through Cloudflare, the Ethernet
cable, or the Pi's own Wi-Fi LAN (the network wlan0 has joined), never from the
Wi-Fi setup network; passwords are never sent back to the browser."""

import asyncio
import datetime
import json
import os
import re
import tempfile

from aiohttp import web

from . import config, terminal

_PAGE = os.path.join(os.path.dirname(__file__), "web", "fleet.html")
_check = {"running": False, "started": None, "output": "", "names": []}
CHOICES = {"access": ["ssh", "console"], "profile": ["linux", "gpu-vera", "bmc-openbmc"], "type": ["", "host", "bmc"],
           "stage": ["", "ES", "QS", "PS", "MP"], "expected_nvlink": ["", "yes", "no"]}
HMC_DEFAULT = "172.31.13.251"


def _dir():
    return f"/home/{config.TERMINAL_USER}/fleet"


def _store():
    return os.path.join(_dir(), "systems.json")


def _guard(req):
    if not terminal.allowed(req):
        raise web.HTTPForbidden(text="fleet: only through Cloudflare, the Ethernet cable or the Pi's Wi-Fi LAN "
                                     "(the list holds passwords)")


def _safe(name):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def _progress():
    """~/fleet/.progress/<name>.json written by fleet.py while it works on a system.
    Work whose process is gone (killed, crashed) shows as "stopped"."""
    out, d = {}, os.path.join(_dir(), ".progress")
    try:
        files = [f for f in os.listdir(d) if f.endswith(".json")]
    except OSError:
        return out
    for f in files:
        try:
            with open(os.path.join(d, f)) as fh:
                p = json.load(fh)
        except (OSError, ValueError):
            continue
        if p.get("state") in ("active", "queued") and not os.path.exists(f"/proc/{p.get('pid')}"):
            p["state"], p["result"] = "stopped", "stopped"
        out[p.get("name")] = p
    return out


def _state():
    out = {"systems": [], "error": None, "check": dict(_check), "choices": CHOICES, "hmc_default": HMC_DEFAULT,
           "tools": os.path.exists(os.path.join(_dir(), "fleet.py"))}
    if not os.path.exists(_store()) and os.path.exists(os.path.join(_dir(), "systems.xlsx")):
        out["error"] = "the old Excel list is moved into the new list on the first check or edit"
    try:
        with open(_store()) as f:
            recs = json.load(f).get("systems", [])
        out["modified"] = int(os.stat(_store()).st_mtime)
    except FileNotFoundError:
        recs = []
    except ValueError as e:
        recs, out["error"] = [], f"systems.json is damaged: {e}"
    prog = _progress()
    out["now"] = datetime.datetime.now().timestamp()
    for r in recs:
        s = {k: v for k, v in r.items() if k != "password"}
        s["progress"] = prog.get(r.get("name"))
        s["password"] = bool(r.get("password"))                  # never the password itself
        try:
            with open(os.path.join(_dir(), "reports", _safe(r.get("name", "")), "latest.json")) as f:
                rep = json.load(f)
            s["flag_list"], s["error"] = rep.get("flags", []), rep.get("error")
            s["seconds"], s["report"] = rep.get("seconds"), True
        except (OSError, ValueError):
            s["report"] = False
        try:
            with open(os.path.join(_dir(), "reports", _safe(r.get("name", "")), "hmc_status.json")) as f:
                s["hmc_status"] = json.load(f)
        except (OSError, ValueError):
            s["hmc_status"] = None
        out["systems"].append(s)
    return out


async def _as_user(*args, timeout=600, stdin=None):
    proc = await asyncio.create_subprocess_exec(
        "/usr/sbin/runuser", "-u", config.TERMINAL_USER, "--", "python3", os.path.join(_dir(), "fleet.py"), *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=_dir(),
        env={"HOME": f"/home/{config.TERMINAL_USER}", "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"})
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "", "timed out"
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def h_page(req):
    _guard(req)
    return web.FileResponse(_PAGE, headers={"Content-Type": "text/html", "Cache-Control": "no-cache"})


async def h_state(req):
    _guard(req)
    return web.json_response(await asyncio.to_thread(_state), headers={"Cache-Control": "no-store"})


async def _edit(req, body):
    rc, out, err = await _as_user("edit", timeout=30, stdin=json.dumps(body).encode())
    if rc:
        msg = (err.strip().splitlines() or [out.strip() or f"exit {rc}"])[-1].removeprefix("fleet: ")
        return web.json_response({"error": msg}, status=400)
    return web.json_response(json.loads(out or "{}"))


async def h_save(req):
    """JSON {old_name: NAME|null, system: {...}, password?: str, clear_password?: bool}."""
    _guard(req)
    try:
        b = await req.json()
    except ValueError:
        return web.json_response({"error": "not JSON"}, status=400)
    body = {"op": "upsert", "old_name": b.get("old_name"), "system": b.get("system") or {},
            "clear_password": bool(b.get("clear_password"))}
    if isinstance(b.get("password"), str) and b["password"] != "":
        body["password"] = b["password"]
    resp = await _edit(req, body)
    if resp.status == 200:
        what = "changed" if b.get("old_name") else "added"
        print(f"fleet: system {(b.get('system') or {}).get('name')} {what} by {terminal._who(req)}", flush=True)
    return resp


async def h_delete(req):
    _guard(req)
    name = req.match_info["name"]
    resp = await _edit(req, {"op": "delete", "name": name})
    if resp.status == 200:
        print(f"fleet: system {name} deleted by {terminal._who(req)}", flush=True)
    return resp


async def h_export(req):
    _guard(req)
    fd, tmp = tempfile.mkstemp(suffix=".xlsx", dir=_dir())
    os.close(fd)
    os.chmod(tmp, 0o666)                     # fleet.py runs as the terminal user and writes it
    try:
        rc, out, err = await _as_user("export", tmp, timeout=60)
        if rc:
            raise web.HTTPInternalServerError(text=err or out)
        with open(tmp, "rb") as f:
            data = f.read()
    finally:
        os.remove(tmp)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    return web.Response(body=data, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="fleet-{stamp}.xlsx"',
                                 "Cache-Control": "no-store"})


async def _run_check(names):
    _check.update(running=True, started=int(datetime.datetime.now().timestamp()), output="", names=names)
    try:
        rc, out, err = await _as_user("check", *names, timeout=3600)     # console systems take minutes each
        _check["output"] = (out + err)[-20000:]
    except Exception as e:                               # noqa: BLE001 - shown on the page
        _check["output"] = f"check could not start: {e}"
    finally:
        _check["running"] = False


async def h_check(req):
    """Optional JSON {names: [...]}: check only those; otherwise all."""
    _guard(req)
    if _check["running"]:
        return web.json_response({"error": "a check is already running"}, status=409)
    names = []
    if req.can_read_body:
        try:
            names = [str(n) for n in (await req.json()).get("names") or []]
        except ValueError:
            pass
    if any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", n) for n in names):
        return web.json_response({"error": "bad system name"}, status=400)
    asyncio.get_running_loop().create_task(_run_check(names))
    print(f"fleet: check of {', '.join(names) or 'all'} started by {terminal._who(req)}", flush=True)
    return web.json_response({"ok": True})


_probing = set()


async def h_hmc_probe(req):
    """Through the BMC row: ping the HMC and GET /redfish/v1/ once (fleet.py hmc-probe)."""
    _guard(req)
    name = req.match_info["name"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", name):
        return web.json_response({"error": "bad system name"}, status=400)
    if name in _probing:
        return web.json_response({"error": "a probe of this system is already running"}, status=409)
    _probing.add(name)
    try:
        rc, out, err = await _as_user("hmc-probe", name, timeout=90)
    finally:
        _probing.discard(name)
    print(f"fleet: HMC probe of {name} by {terminal._who(req)}: exit {rc}", flush=True)
    if rc and not out.strip():
        return web.json_response({"error": (err.strip().splitlines() or [f"exit {rc}"])[-1].removeprefix("fleet: ")},
                                 status=502)
    return web.json_response(json.loads(out.strip().splitlines()[-1]))


async def h_report(req):
    _guard(req)
    path = os.path.join(_dir(), "reports", _safe(req.match_info["name"]), "latest.txt")
    if not os.path.exists(path):
        raise web.HTTPNotFound(text="no report yet")
    return web.FileResponse(path, headers={"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store"})


ROUTES = [web.get("/fleet", h_page), web.get("/api/fleet", h_state),
          web.post("/api/fleet/systems", h_save), web.delete("/api/fleet/systems/{name}", h_delete),
          web.get("/api/fleet/export", h_export), web.post("/api/fleet/check", h_check),
          web.post("/api/fleet/hmc-probe/{name}", h_hmc_probe), web.get("/api/fleet/report/{name}", h_report)]
