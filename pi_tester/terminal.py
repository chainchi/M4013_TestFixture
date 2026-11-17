"""Web terminal: /terminal opens the Pi's shared tmux session (`shared`, the one
tools/pirun.sh types into) in the browser, as TERMINAL_USER. Several pages and
SSH users can be attached at once and all see and type into the same shell.
The page can also open extra terminals (tmux sessions t1, t2, ...) and kill
them; the shared one cannot be killed.

It is a full shell with sudo and the tester has no login, so it is only served
to requests that come through the Cloudflare tunnel (cloudflared connects from
loopback, after Cloudflare Access checked the user), over the direct Ethernet
link, or over the Wi-Fi LAN wlan0 has joined; the Wi-Fi setup network (open to
anyone nearby) is refused."""

import asyncio
import fcntl
import ipaddress
import json
import os
import pty
import re
import signal
import struct
import subprocess
import termios
import time

from aiohttp import WSMsgType, web

from . import config, net, settings

MAX_SESSIONS = 8                                     # extra terminals besides the shared one
TMUX_UNIT = "pi-tester-tmux.service"
_PAGE = os.path.join(os.path.dirname(__file__), "web", "terminal.html")
_DENIED = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Terminal</title><body style="font:18px/1.5 system-ui,sans-serif;max-width:640px;margin:40px auto;padding:0 16px">
<h1>Terminal not available here</h1>
<p>The terminal is a full shell on the Pi, so it only opens through
<b>https://pi-tester.dona-anna.com/terminal</b> (after the Cloudflare login) or over
the direct Ethernet cable (<b>http://192.168.10.2/terminal</b>), or from the
Wi-Fi network the Pi has joined. The Wi-Fi setup network is not allowed.</p>
<p>You reached the Pi from {peer}, which is not one of those.</p>
<p><a href="/">Back to the test host</a></p></body>"""


_wlan = {"ip": "0.0.0.0", "at": 0.0}


def wifi_lan(req):
    """Came in on wlan0's address while wlan0 is a normal Wi-Fi client (not the
    setup network, where anyone who knows its name can join)."""
    if time.monotonic() - _wlan["at"] > 10:              # pages poll every second
        _wlan.update(ip=net.ipv4(config.WIFI_IF), at=time.monotonic())
    sock = req.transport and req.transport.get_extra_info("sockname")
    ip = _wlan["ip"]
    return bool(sock) and ip not in ("0.0.0.0", config.WIFI_SETUP_IP) and sock[0] == ip


def allowed(req):
    """Loopback = cloudflared (or a process on the Pi itself); the Wi-Fi LAN (not
    the setup network); else the peer must be on the Ethernet subnet and have
    come in on the Ethernet address."""
    peer = req.remote or ""
    try:
        ip = ipaddress.ip_address(peer.split("%")[0])
    except ValueError:
        return False
    if ip.is_loopback or wifi_lan(req):
        return True
    s = settings.get()
    if not s["static_ip"]:
        return False
    sock = req.transport and req.transport.get_extra_info("sockname")
    eth = ipaddress.ip_network(f"{s['ip']}/{s['netmask']}", strict=False)
    return ip.version == 4 and ip in eth and bool(sock) and sock[0] == s["ip"]


def _who(req):
    return req.headers.get("Cf-Access-Authenticated-User-Email") or req.remote or "?"


async def h_page(req):
    if not allowed(req):
        return web.Response(status=403, content_type="text/html", text=_DENIED.format(peer=req.remote))
    return web.FileResponse(_PAGE, headers={"Content-Type": "text/html", "Cache-Control": "no-cache"})


_NAME = re.compile(r"^[A-Za-z0-9_-]{1,20}$")


def _env():
    user = config.TERMINAL_USER
    home = f"/home/{user}"
    return home, {"HOME": home, "USER": user, "LOGNAME": user, "SHELL": "/bin/bash",
                  "TERM": "xterm-256color", "LANG": "C.UTF-8",
                  "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"}


def _server():
    """The tmux server must run in pi-tester-tmux.service: one started from here
    would live in this service's cgroup and die with every service restart."""
    if subprocess.run(["systemctl", "is-active", "--quiet", TMUX_UNIT]).returncode:
        subprocess.run(["systemctl", "start", TMUX_UNIT], timeout=15)


def _tmux(*args):
    """Run a tmux command as TERMINAL_USER (same tmux server as the sessions)."""
    _server()
    home, env = _env()
    return subprocess.run(["runuser", "-u", config.TERMINAL_USER, "--", "tmux", *args],
                          capture_output=True, text=True, timeout=5, env=env, cwd=home)


def _sessions():
    r = _tmux("list-sessions", "-F", "#{session_name}\t#{session_attached}\t#{session_created}\t#{pane_current_command}")
    out = []
    for line in r.stdout.splitlines() if r.returncode == 0 else []:
        name, attached, created, cmd = (line.split("\t") + ["", "", ""])[:4]
        out.append({"name": name, "viewers": int(attached or 0), "created": int(created or 0), "command": cmd,
                     "shared": name == config.TERMINAL_SESSION})
    out.sort(key=lambda s: (not s["shared"], s["created"]))
    return out


def _spawn(session):
    """tmux client for `session` in a new pty, as TERMINAL_USER. The shared
    session is created if missing; others must exist (a killed one stays gone)."""
    _server()
    home, env = _env()
    cmd = (["new-session", "-A", "-s", session] if session == config.TERMINAL_SESSION
           else ["attach-session", "-t", "=" + session])
    pid, fd = pty.fork()
    if pid == 0:                                     # child: only exec from here
        try:
            os.chdir(home)
            os.execvpe("runuser", ["runuser", "-u", config.TERMINAL_USER, "--", "tmux", *cmd], env)
        finally:
            os._exit(127)
    os.set_blocking(fd, False)
    return pid, fd


async def _write(fd, data):
    """The pty is non-blocking; a big paste can fill its buffer."""
    while data:
        try:
            data = data[os.write(fd, data):]
        except BlockingIOError:
            await asyncio.sleep(0.01)


def _reap(pid):
    """Closing the pty hangs up the tmux client; make sure it is gone."""
    for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            break
        for _ in range(20):
            if os.waitpid(pid, os.WNOHANG)[0]:
                return
            time.sleep(0.05)
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


def _resize(fd, cols, rows):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


async def h_list(req):
    if not allowed(req):
        raise web.HTTPForbidden(text="terminal: not from this network")
    return web.json_response({"sessions": await asyncio.to_thread(_sessions), "max": MAX_SESSIONS})


async def h_new(req):
    """New terminal: the next free name t1, t2, ..."""
    if not allowed(req):
        raise web.HTTPForbidden(text="terminal: not from this network")
    have = {s["name"] for s in await asyncio.to_thread(_sessions)}
    if len(have - {config.TERMINAL_SESSION}) >= MAX_SESSIONS:
        return web.json_response({"error": f"at most {MAX_SESSIONS} extra terminals"}, status=409)
    name = next(f"t{i}" for i in range(1, 100) if f"t{i}" not in have)
    r = await asyncio.to_thread(_tmux, "new-session", "-d", "-s", name, "-x", "160", "-y", "45")
    if r.returncode:
        return web.json_response({"error": r.stderr.strip() or "tmux failed"}, status=500)
    print(f"terminal: {name} created by {_who(req)}", flush=True)
    return web.json_response({"name": name})


async def h_kill(req):
    if not allowed(req):
        raise web.HTTPForbidden(text="terminal: not from this network")
    name = req.match_info["name"]
    if name == config.TERMINAL_SESSION:
        return web.json_response({"error": "the shared terminal is used by Claude and pirun.sh; it cannot be killed"},
                                 status=409)
    if not _NAME.match(name):
        return web.json_response({"error": "bad name"}, status=400)
    r = await asyncio.to_thread(_tmux, "kill-session", "-t", "=" + name)
    if r.returncode:
        return web.json_response({"error": r.stderr.strip() or "no such terminal"}, status=404)
    print(f"terminal: {name} killed by {_who(req)}", flush=True)
    return web.json_response({"ok": True})


async def h_ws(req):
    if not allowed(req):
        raise web.HTTPForbidden(text="terminal: not from this network")
    session = req.query.get("session") or config.TERMINAL_SESSION
    if not _NAME.match(session):
        raise web.HTTPBadRequest(text="terminal: bad session name")
    if session != config.TERMINAL_SESSION and not any(s["name"] == session for s in await asyncio.to_thread(_sessions)):
        raise web.HTTPNotFound(text="terminal: no such session")
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=64 * 1024)
    await ws.prepare(req)
    pid, fd = await asyncio.to_thread(_spawn, session)
    print(f"terminal: {session} opened by {_who(req)}", flush=True)     # audit trail in the journal
    loop = asyncio.get_running_loop()
    out = asyncio.Queue()

    def readable():
        try:
            data = os.read(fd, 65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""                               # EIO: the tmux client exited
        out.put_nowait(data)
        if not data:
            loop.remove_reader(fd)

    async def pump():
        while (data := await out.get()):
            await ws.send_bytes(data)
        await ws.close(code=1000, message=b"session ended")

    loop.add_reader(fd, readable)
    sender = asyncio.create_task(pump())
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:         # keystrokes
                await _write(fd, msg.data)
            elif msg.type == WSMsgType.TEXT:         # {"resize": [cols, rows]}
                try:
                    cols, rows = json.loads(msg.data)["resize"]
                    _resize(fd, max(20, min(int(cols), 500)), max(5, min(int(rows), 200)))
                except (ValueError, KeyError, TypeError, OSError):
                    pass
    except OSError:
        pass
    finally:
        sender.cancel()
        loop.remove_reader(fd)
        os.close(fd)                                 # detaches this client; the session stays
        await asyncio.to_thread(_reap, pid)
        print(f"terminal: {session} closed by {_who(req)}", flush=True)
    return ws


ROUTES = [web.get("/terminal", h_page), web.get("/ws/terminal", h_ws),
          web.get("/api/terminal/sessions", h_list), web.post("/api/terminal/sessions", h_new),
          web.delete("/api/terminal/sessions/{name}", h_kill)]
