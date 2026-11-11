"""REST API, WebSocket consoles and UART streams. Same endpoints and JSON as
the P4 firmware, so the web page and existing scripts work unchanged."""

import asyncio
import os
import re
import subprocess
import time

from aiohttp import WSMsgType, web

from . import agent, camera, config, i2c_ctrl, io_ctrl, net, redfish, settings, system_view, terminal, uart_hub, wifi_setup

WS_CHUNK = 4096
PUMP_PERIOD_S = 0.03
STREAM_POLL_S = 0.05
MAX_STREAMS = 4

_INDEX = os.path.join(os.path.dirname(__file__), "web", "index.html")
_CAMERA = os.path.join(os.path.dirname(__file__), "web", "camera.html")

_ws_clients = set()
# Live-stream cursors, only touched from the event loop (pump and the WS handler)
_cursor = [0] * config.UART_PORTS
_streams = 0

# ---------- helpers ----------


def result(err=None):
    return web.json_response({"ok": True} if err is None else {"ok": False, "error": str(err)})


async def read_json(req):
    try:
        j = await req.json()
    except ValueError:
        j = None
    if not isinstance(j, dict):
        raise web.HTTPBadRequest(text="json body required")
    return j


def parse_hex_bytes(s, max_len=256):
    """"0x12 34,ab" -> b"\\x12\\x34\\xab". Returns None on a bad token."""
    out = bytearray()
    for tok in re.split(r"[\s,]+", (s or "").strip()):
        if not tok:
            continue
        if tok[:2].lower() == "0x":
            tok = tok[2:]
        try:
            v = int(tok, 16)
        except ValueError:
            return None
        if not 0 <= v <= 0xff or len(out) >= max_len:
            return None
        out.append(v)
    return bytes(out)


def query_port(req):
    try:
        port = int(req.query.get("port", "-1"))
    except ValueError:
        port = -1
    if not 0 <= port < config.UART_PORTS:
        raise web.HTTPBadRequest(text="port=0..3 required")
    return port

# ---------- page ----------


async def h_index(req):
    return web.FileResponse(_INDEX, headers={"Content-Type": "text/html", "Cache-Control": "no-cache"})


async def h_camera_page(req):
    return web.FileResponse(_CAMERA, headers={"Content-Type": "text/html", "Cache-Control": "no-cache"})

# ---------- status ----------


async def h_status(req):
    s = settings.get()
    wifi_ok, rssi = await asyncio.to_thread(net.wifi_status)
    return web.json_response({
        "gpio_out": [io_ctrl.gpio_out_get(i) for i in range(io_ctrl.OUT_COUNT)],
        "gpio_in": [io_ctrl.gpio_in_get(i) for i in range(io_ctrl.IN_COUNT)],
        "pwm": [io_ctrl.pwm_get(i) for i in range(io_ctrl.PWM_COUNT)],
        "uart": [{"connected": uart_hub.connected(i), "baud": uart_hub.baud(i), "dev": uart_hub.device(i)}
                 for i in range(config.UART_PORTS)],
        "usb_dev": uart_hub.last_usb_id(),
        "net": {"link": net.link_up(), "ip": net.ipv4(config.ETH_IF), "dut": s["dut_host"]},
        "wifi": {"ssid": s["wifi_ssid"], "connected": wifi_ok, "ip": net.ipv4(config.WIFI_IF), "rssi": rssi},
    })

# ---------- gpio / pwm ----------


async def h_gpio(req):
    j = await read_json(req)
    try:
        io_ctrl.gpio_out_set(int(j.get("ch", -1)), int(j.get("level", 0)))
    except (ValueError, TypeError) as e:
        return result(e)
    return result()


async def h_pwm(req):
    j = await read_json(req)
    try:
        ch = int(j.get("ch", -1))
        cur = io_ctrl.pwm_get(ch) if 0 <= ch < io_ctrl.PWM_COUNT else io_ctrl.PwmState().__dict__
        # omitted fields keep their current value
        en = j.get("enable")
        io_ctrl.pwm_set(ch, en if isinstance(en, bool) else cur["enable"],
                        float(j.get("freq", cur["freq"])), float(j.get("duty", cur["duty"])))
    except (ValueError, TypeError, OSError) as e:
        return result(e)
    return result()

# ---------- i2c ----------


async def h_i2c_scan(req):
    try:
        j = await req.json()
    except ValueError:
        j = {}
    bus = int((j or {}).get("bus", 0))
    if not 0 <= bus < i2c_ctrl.BUS_COUNT:
        raise web.HTTPBadRequest(text="bus must be 0-1")
    sda, scl = await asyncio.to_thread(i2c_ctrl.line_levels, bus)
    r = {"sda": sda, "scl": scl, "speed": i2c_ctrl.bus_speed(bus)}
    try:
        r.update(ok=True, found=await asyncio.to_thread(i2c_ctrl.scan, bus))
    except OSError as e:
        print(f"i2c: bus {bus} scan: {e}", flush=True)
        r.update(ok=False, found=[], error="bus error (check pull-ups / SDA-SCL short)")
    return web.json_response(r)


async def h_i2c_xfer(req):
    j = await read_json(req)
    w = parse_hex_bytes(j.get("write"))
    try:
        bus, addr, rlen = int(j.get("bus", 0)), int(j.get("addr", -1)), int(j.get("read", 0))
    except (ValueError, TypeError):
        bus = addr = rlen = -1
    if w is None or not 0 <= rlen <= 256 or addr < 0:
        raise web.HTTPBadRequest(text="bad write bytes, read length or address")
    o = {}
    try:
        data = await asyncio.to_thread(i2c_ctrl.xfer, bus, addr, w, rlen)
        o["ok"] = True
        if rlen:
            o["data"] = " ".join(f"{b:02X}" for b in data)
    except i2c_ctrl.Nack:
        o = {"ok": False, "error": "NACK / no device"}
    except (ValueError, OSError) as e:
        o = {"ok": False, "error": str(e)}
    speed = j.get("speed")
    if isinstance(speed, int) and speed != i2c_ctrl.bus_speed(bus if 0 <= bus < i2c_ctrl.BUS_COUNT else 0):
        o["note"] = f"bus runs at its fixed {i2c_ctrl.bus_speed(bus)} Hz"
    return web.json_response(o)

# ---------- uart ----------


async def h_uart_cfg(req):
    j = await read_json(req)
    try:
        port = int(j.get("port", -1))
        if isinstance(j.get("baud"), (int, float)):
            uart_hub.set_baud(port, int(j["baud"]))
        if j.get("clear") is True:
            uart_hub.clear(port)
    except (ValueError, TypeError, OSError) as e:
        return result(e)
    return result()


async def h_uart_log(req):
    """The port's whole scrollback as a text file."""
    port = query_port(req)
    resp = web.StreamResponse(headers={
        "Content-Type": "text/plain",
        "Content-Disposition": f'attachment; filename="dut_uart{port}.log"'})
    await resp.prepare(req)
    pos, end = 0, uart_hub.rx_total(port)
    while pos < end:
        data, pos = uart_hub.read_since(port, pos, WS_CHUNK)
        if not data:
            break
        await resp.write(data)
    await resp.write_eof()
    return resp


async def h_uart_stream(req):
    """Endless text/plain response carrying the port's received bytes live
    (curl -N). history=1 starts at the beginning of the scrollback."""
    global _streams
    port = query_port(req)
    if _streams >= MAX_STREAMS:
        return web.Response(status=503, text="too many UART streams open\n")
    pos = 0 if req.query.get("history", "0") not in ("", "0") else uart_hub.rx_total(port)
    resp = web.StreamResponse(headers={"Content-Type": "text/plain", "Cache-Control": "no-cache"})
    await resp.prepare(req)
    _streams += 1
    try:
        while True:
            data, pos = uart_hub.read_since(port, pos, WS_CHUNK)
            if data:
                await resp.write(data)
            else:
                await asyncio.sleep(STREAM_POLL_S)
    except (ConnectionResetError, asyncio.CancelledError):
        pass                           # client went away
    finally:
        _streams -= 1
    return resp


async def _ws_send_all(frame):
    for ws in list(_ws_clients):
        try:
            await ws.send_bytes(frame)
        except ConnectionResetError:
            _ws_clients.discard(ws)


async def _pump():
    """Forwards new UART bytes to every WS client. Frame = [port][bytes...]."""
    while True:
        await asyncio.sleep(PUMP_PERIOD_S)
        for p in range(config.UART_PORTS):
            while True:
                data, _cursor[p] = uart_hub.read_since(p, _cursor[p], WS_CHUNK)
                if not data:
                    break
                await _ws_send_all(bytes([p]) + data)


async def h_ws(req):
    """Client -> server: binary [port][keystrokes], or text "hello" to get
    every port's scrollback (sent before any newer live data)."""
    ws = web.WebSocketResponse()
    await ws.prepare(req)
    _ws_clients.add(ws)
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY and len(msg.data) > 1:
                try:
                    await asyncio.to_thread(uart_hub.write, msg.data[0], msg.data[1:])
                except (ValueError, ConnectionError, OSError):
                    pass
            elif msg.type == WSMsgType.TEXT and msg.data == "hello":
                for p in range(config.UART_PORTS):
                    pos = 0
                    # stop at the live cursor; later bytes arrive through _pump
                    while pos < _cursor[p]:
                        data, pos = uart_hub.read_since(p, pos, min(WS_CHUNK, _cursor[p] - pos))
                        if not data:
                            break
                        await ws.send_bytes(bytes([p]) + data)
    finally:
        _ws_clients.discard(ws)
    return ws

# ---------- camera ----------


async def h_camera_status(req):
    return web.json_response(await asyncio.to_thread(camera.cam.status))


async def h_camera_stream(req):
    """MJPEG (multipart/x-mixed-replace) for an <img>. fps=N caps the rate
    (saves bandwidth through the tunnel)."""
    cam = camera.cam
    if cam.viewers >= config.CAMERA_MAX_VIEWERS:
        return web.Response(status=503, text="too many camera viewers\n")
    try:
        fps = max(1, min(30, int(req.query.get("fps", "30"))))
    except ValueError:
        fps = 30
    gap = 1 / fps
    resp = web.StreamResponse(headers={
        "Content-Type": "multipart/x-mixed-replace; boundary=frame",
        "Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"})
    await resp.prepare(req)
    cam.acquire(fps)
    seq, sent = cam.seq, 0.0
    try:
        while True:
            jpeg, seq = await cam.next_frame(seq)
            if not jpeg:
                cam.start()                  # unplugged/stopped: retry once a second
                await asyncio.sleep(1)
                continue
            now = asyncio.get_running_loop().time()
            if now - sent < gap * 0.8:          # frames come at camera-frame boundaries
                continue
            sent = now
            await resp.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpeg)
                             + jpeg + b"\r\n")
    except (ConnectionResetError, asyncio.CancelledError):
        pass                                 # viewer went away
    finally:
        cam.release(fps)
    return resp


async def h_camera_snapshot(req):
    cam = camera.cam
    cold = not cam.running()
    cam.acquire()
    try:
        if cold:                             # let auto-exposure settle first
            await asyncio.sleep(1.5)
        jpeg, _ = await cam.next_frame(cam.seq)
    finally:
        cam.release()
    if not jpeg:
        return web.Response(status=503, text=(cam.error or "no frame from the camera") + "\n")
    name = "camera_" + time.strftime("%Y%m%d_%H%M%S") + ".jpg"
    return web.Response(body=jpeg, headers={
        "Content-Type": "image/jpeg", "Cache-Control": "no-store",
        "Content-Disposition": f'inline; filename="{name}"'})

# ---------- redfish ----------


async def h_redfish(req):
    j = await read_json(req)
    body = j.get("body") or None
    return web.json_response(await redfish.request(j.get("method"), j.get("path") or "/redfish/v1", body))

# ---------- settings ----------


async def h_settings_get(req):
    s = settings.get()
    out = {k: v for k, v in s.items() if k not in ("dut_pass", "wifi_pass")}
    out["dut_pass_set"] = bool(s["dut_pass"])       # never echo the passwords
    out["wifi_pass_set"] = bool(s["wifi_pass"])
    return web.json_response(out)


async def h_settings_set(req):
    j = await read_json(req)
    old = settings.get()
    s = dict(old)
    for k in ("static_ip", "dut_https"):
        if isinstance(j.get(k), bool):
            s[k] = j[k]
    for k in ("ip", "netmask", "gateway", "dut_host", "dut_user", "wifi_ssid"):
        if isinstance(j.get(k), str):
            s[k] = j[k].strip()
    for k in ("wifi_pass", "dut_pass"):                 # empty = keep the stored password
        if isinstance(j.get(k), str) and j[k]:
            s[k] = j[k]
    try:
        if any(s[k] != old[k] for k in net.NETWORK_KEYS):
            await asyncio.to_thread(net.write_netplan, s)
        settings.save(s)
    except (ValueError, OSError) as e:
        return result(e)
    return result()


async def h_reboot(req):
    asyncio.get_running_loop().call_later(0.5, subprocess.Popen, ["systemctl", "reboot"])
    return result()

# ---------- start ----------


async def _on_startup(app):
    app["pump"] = asyncio.create_task(_pump())
    await agent.start(app)


async def _on_shutdown(app):
    # consoles and the agent page stay connected for good; close them so a
    # restart doesn't wait for the browsers
    for ws in list(_ws_clients) + list(agent.agent.clients):
        await ws.close(code=1001, message=b"server restarting")


def main():
    settings.init()
    # DUT-facing pins first so outputs go to a defined (low) state early
    io_ctrl.init()
    uart_hub.init()

    app = web.Application(client_max_size=8192, middlewares=[wifi_setup.captive])
    app.add_routes([
        web.get("/", h_index),
        web.get("/api/status", h_status),
        web.post("/api/gpio", h_gpio),
        web.post("/api/pwm", h_pwm),
        web.post("/api/i2c/scan", h_i2c_scan),
        web.post("/api/i2c/xfer", h_i2c_xfer),
        web.post("/api/uart", h_uart_cfg),
        web.get("/api/uart/log", h_uart_log),
        web.get("/api/uart/stream", h_uart_stream),
        web.get("/camera", h_camera_page),
        web.get("/api/camera", h_camera_status),
        web.get("/api/camera/stream", h_camera_stream),
        web.get("/api/camera/snapshot", h_camera_snapshot),
        web.post("/api/redfish", h_redfish),
        web.get("/api/settings", h_settings_get),
        web.post("/api/settings", h_settings_set),
        web.post("/api/reboot", h_reboot),
        web.get("/ws", h_ws),
        *agent.ROUTES,
        *system_view.ROUTES,
        *wifi_setup.ROUTES,
        *terminal.ROUTES,
        web.static("/vendor", os.path.join(os.path.dirname(__file__), "web", "vendor")),
    ])
    system_view.extra = lambda: {"console_pages": len(_ws_clients), "uart_streams": _streams,
                                 "camera_viewers": camera.cam.viewers}
    app.on_startup.append(_on_startup)
    app.on_startup.append(wifi_setup.on_startup)
    app.on_shutdown.append(_on_shutdown)
    print(f"web: serving on port {config.HTTP_PORT}", flush=True)
    # endless camera/UART streams are cancelled after this, not waited for
    web.run_app(app, port=config.HTTP_PORT, print=None, shutdown_timeout=3)
