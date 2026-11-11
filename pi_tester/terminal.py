"""Web terminal: /terminal opens the Pi's shared tmux session (`shared`, the one
tools/pirun.sh types into) in the browser, as TERMINAL_USER. Several pages and
SSH users can be attached at once and all see and type into the same shell.

It is a full shell with sudo and the tester has no login, so it is only served
to requests that come through the Cloudflare tunnel (cloudflared connects from
loopback, after Cloudflare Access checked the user) or over the direct
Ethernet link; the Wi-Fi LAN and the setup network are refused."""

import asyncio
import fcntl
import ipaddress
import json
import os
import pty
import signal
import struct
import termios
import time

from aiohttp import WSMsgType, web

from . import config, settings

_PAGE = os.path.join(os.path.dirname(__file__), "web", "terminal.html")
_DENIED = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Terminal</title><body style="font:18px/1.5 system-ui,sans-serif;max-width:640px;margin:40px auto;padding:0 16px">
<h1>Terminal not available here</h1>
<p>The terminal is a full shell on the Pi, so it only opens through
<b>https://pi-tester.dona-anna.com/terminal</b> (after the Cloudflare login) or over
the direct Ethernet cable (<b>http://192.168.10.2/terminal</b>).</p>
<p>You reached the Pi from {peer}, which is not one of those.</p>
<p><a href="/">Back to the test host</a></p></body>"""


def allowed(req):
    """Loopback = cloudflared (or a process on the Pi itself); else the peer must
    be on the Ethernet subnet and have come in on the Ethernet address."""
    peer = req.remote or ""
    try:
        ip = ipaddress.ip_address(peer.split("%")[0])
    except ValueError:
        return False
    if ip.is_loopback:
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


def _spawn():
    """tmux client for the shared session in a new pty, as TERMINAL_USER."""
    user = config.TERMINAL_USER
    home = f"/home/{user}"
    pid, fd = pty.fork()
    if pid == 0:                                     # child: only exec from here
        try:
            os.chdir(home)
            os.execvpe("runuser", ["runuser", "-u", user, "--", "tmux", "new-session", "-A", "-s",
                                   config.TERMINAL_SESSION],
                       {"HOME": home, "USER": user, "LOGNAME": user, "SHELL": "/bin/bash",
                        "TERM": "xterm-256color", "LANG": "C.UTF-8", "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"})
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


async def h_ws(req):
    if not allowed(req):
        raise web.HTTPForbidden(text="terminal: not from this network")
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=64 * 1024)
    await ws.prepare(req)
    pid, fd = await asyncio.to_thread(_spawn)
    print(f"terminal: opened by {_who(req)}", flush=True)     # audit trail in the journal
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
        print(f"terminal: closed by {_who(req)}", flush=True)
    return ws


ROUTES = [web.get("/terminal", h_page), web.get("/ws/terminal", h_ws)]
