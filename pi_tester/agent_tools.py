"""The agent's hardware tools: the same functions the web page drives.
Each tool says whether it changes the DUT; those wait for the user's approval."""

import asyncio
import base64
import json
import os
import re
import signal

from . import camera, config, i2c_ctrl, io_ctrl, net, redfish, settings, uart_hub

MAX_RESULT = 8000                      # characters of tool output given to the model
_uart_seen = [0] * config.UART_PORTS   # agent's own "new output" cursor per port


def _hex(s):
    s = re.sub(r"0[xX]|[\s,]", "", s or "")
    return bytes.fromhex(s)


def _clip(s):
    return s if len(s) <= MAX_RESULT else s[:MAX_RESULT] + f"\n... ({len(s) - MAX_RESULT} more characters cut)"


def _int(v, lo, hi, name):
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number")
    if not lo <= v <= hi:
        raise ValueError(f"{name} must be {lo}..{hi}")
    return v

# ---------- tools ----------


async def get_status(a):
    s = settings.get()
    return json.dumps({
        "gpio_out": [io_ctrl.gpio_out_get(i) for i in range(io_ctrl.OUT_COUNT)],
        "gpio_in": [io_ctrl.gpio_in_get(i) for i in range(io_ctrl.IN_COUNT)],
        "pwm": [io_ctrl.pwm_get(i) for i in range(io_ctrl.PWM_COUNT)],
        "uart": [{"port": i, "connected": uart_hub.connected(i), "baud": uart_hub.baud(i)}
                 for i in range(config.UART_PORTS)],
        "ethernet": {"link": net.link_up(), "ip": net.ipv4(config.ETH_IF)},
        "dut_host": s["dut_host"],
        "camera_present": bool(camera.find_device()[0]),
    })


async def set_gpio(a):
    ch, level = _int(a.get("ch"), 0, io_ctrl.OUT_COUNT - 1, "ch"), _int(a.get("level"), 0, 1, "level")
    io_ctrl.gpio_out_set(ch, level)
    return f"OUT{ch} = {level}"


async def set_pwm(a):
    ch = _int(a.get("ch"), 0, io_ctrl.PWM_COUNT - 1, "ch")
    cur = io_ctrl.pwm_get(ch)
    en = a.get("enable", cur["enable"])
    io_ctrl.pwm_set(ch, bool(en), float(a.get("freq", cur["freq"])), float(a.get("duty", cur["duty"])))
    return json.dumps(io_ctrl.pwm_get(ch))


async def i2c_scan(a):
    bus = _int(a.get("bus"), 0, i2c_ctrl.BUS_COUNT - 1, "bus")
    found = await asyncio.to_thread(i2c_ctrl.scan, bus)
    return "found: " + (" ".join(f"0x{x:02X}" for x in found) or "no devices")


async def i2c_transfer(a):
    bus = _int(a.get("bus"), 0, i2c_ctrl.BUS_COUNT - 1, "bus")
    addr = _int(a.get("addr"), 0, 0x7F, "addr")
    rlen = _int(a.get("read_len", 0), 0, 256, "read_len")
    w = _hex(a.get("write_hex", ""))
    try:
        data = await asyncio.to_thread(i2c_ctrl.xfer, bus, addr, w, rlen)
    except i2c_ctrl.Nack:
        raise ValueError(f"NACK / no device at 0x{addr:02X} on bus {bus}")
    return ("read: " + " ".join(f"{b:02X}" for b in data)) if rlen else "write ok"


def _i2c_writes(a):
    # write-then-read (register pointer + repeated start) only reads; anything else may change the device
    try:
        return int(a.get("read_len", 0)) == 0 or len(_hex(a.get("write_hex", ""))) > 1
    except (TypeError, ValueError):
        return True


async def uart_read(a):
    port = _int(a.get("port"), 0, config.UART_PORTS - 1, "port")
    n = _int(a.get("max_bytes", 4000), 1, MAX_RESULT, "max_bytes")
    end = uart_hub.rx_total(port)
    pos = max(_uart_seen[port], end - n) if a.get("only_new") else max(0, end - n)
    out = bytearray()
    while pos < end:
        data, pos = uart_hub.read_since(port, pos, end - pos)
        if not data:
            break
        out += data
    _uart_seen[port] = end
    text = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", out.decode(errors="replace")).replace("\r", "")
    state = "connected" if uart_hub.connected(port) else "NOT connected"
    return f"[UART {port}, {state}, {len(out)} bytes]\n{text or '(no output)'}"


async def uart_send(a):
    port = _int(a.get("port"), 0, config.UART_PORTS - 1, "port")
    text = str(a.get("text", ""))
    if a.get("enter", True):
        text += "\r"
    await asyncio.to_thread(uart_hub.write, port, text.encode())
    return f"sent {len(text)} bytes to UART {port}"


async def redfish_request(a):
    r = await redfish.request(a.get("method", "GET"), a.get("path") or "/redfish/v1", a.get("body") or None)
    return _clip(json.dumps(r))


async def camera_snapshot(a):
    cam = camera.cam
    cold = not cam.running()
    cam.acquire()
    try:
        if cold:
            await asyncio.sleep(1.5)       # auto-exposure
        jpeg, _ = await cam.next_frame(cam.seq)
    finally:
        cam.release()
    if not jpeg:
        raise ValueError(cam.error or "no frame from the camera")
    return [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.b64encode(jpeg).decode()}},
            {"type": "text", "text": f"camera frame {cam.width}x{cam.height}"}]


async def shell(a):
    """bash -lc as the login user; output and exit code back to the model."""
    cmd = str(a.get("command", "")).strip()
    if not cmd:
        raise ValueError("command is empty")
    t = _int(a.get("timeout_s", 60), 1, 600, "timeout_s")
    home = f"/home/{config.AGENT_SHELL_USER}"
    print(f"agent shell ({config.AGENT_SHELL_USER}): {cmd}", flush=True)   # audit trail in the journal
    p = await asyncio.create_subprocess_exec(
        "runuser", "-u", config.AGENT_SHELL_USER, "--", "bash", "-lc", cmd,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        cwd=home if os.path.isdir(home) else "/", start_new_session=True,
        env={"HOME": home, "USER": config.AGENT_SHELL_USER, "LANG": "C.UTF-8", "TERM": "dumb",
             "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"})
    try:
        out, _ = await asyncio.wait_for(p.communicate(), t)
        rc = f"exit code {p.returncode}"
    except TimeoutError:
        out, rc = b"", f"killed after {t} s (timeout)"
    finally:
        if p.returncode is None:       # timeout or Stop: kill the whole process group
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await p.wait()
    text = out.decode(errors="replace")
    if len(text) > MAX_RESULT:         # keep the start and the end, where errors usually are
        text = text[:MAX_RESULT // 2] + f"\n... ({len(text) - MAX_RESULT} characters cut) ...\n" + text[-MAX_RESULT // 2:]
    return f"[{rc}]\n{text or '(no output)'}"


async def wait(a):
    s = _int(a.get("seconds"), 1, 120, "seconds")
    await asyncio.sleep(s)
    return f"waited {s} s"

# ---------- definitions ----------


def _schema(props, required=()):
    return {"type": "object", "properties": props, "required": list(required)}


_N = {"type": "integer"}
TOOLS = [
    ("get_status", get_status, False, "Read the tester's state: GPIO outputs/inputs, PWM, UART ports, "
     "Ethernet, DUT host, camera present.", _schema({})),
    ("set_gpio", set_gpio, True, "Drive a GPIO output (OUT0 = header pin 11, OUT1 = pin 13) high or low.",
     _schema({"ch": {**_N, "description": "0 or 1"}, "level": {**_N, "description": "0 low, 1 high"}},
             ["ch", "level"])),
    ("set_pwm", set_pwm, True, "Set a hardware PWM channel (PWM0 = pin 12, PWM1 = pin 35). "
     "Omitted fields keep their value.",
     _schema({"ch": _N, "enable": {"type": "boolean"}, "freq": {"type": "number", "description": "Hz, 1..1000000"},
              "duty": {"type": "number", "description": "percent 0..100"}}, ["ch"])),
    ("i2c_scan", i2c_scan, False, "List 7-bit addresses that answer on I2C bus 0 (pins 3/5) or 1 (pins 7/29).",
     _schema({"bus": _N}, ["bus"])),
    ("i2c_transfer", i2c_transfer, _i2c_writes,
     "Raw I2C transfer: write bytes, then read read_len bytes with a repeated start. "
     "Write-1-byte-then-read is a register/command read; anything else is a write and needs approval. "
     "No PMBus PEC is added.",
     _schema({"bus": _N, "addr": {**_N, "description": "7-bit address, e.g. 64 for 0x40"},
              "write_hex": {"type": "string", "description": "bytes to write, e.g. \"8B\" or \"D0 E8 0B\""},
              "read_len": {**_N, "description": "bytes to read, 0..256"}}, ["bus", "addr"])),
    ("uart_read", uart_read, False, "Read DUT console output from a UART port's scrollback "
     "(the latest max_bytes, or only what arrived since your last read).",
     _schema({"port": _N, "max_bytes": _N, "only_new": {"type": "boolean"}}, ["port"])),
    ("uart_send", uart_send, True, "Type text into a DUT UART console (Enter is added unless enter=false).",
     _schema({"port": _N, "text": {"type": "string"}, "enter": {"type": "boolean"}}, ["port", "text"])),
    ("redfish_request", redfish_request, lambda a: str(a.get("method", "GET")).upper() != "GET",
     "Send a Redfish request to the DUT's BMC (host and credentials from Settings). GET is read-only.",
     _schema({"method": {"type": "string", "enum": ["GET", "POST", "PATCH", "PUT", "DELETE"]},
              "path": {"type": "string"}, "body": {"type": "string", "description": "JSON text"}}, ["path"])),
    ("camera_snapshot", camera_snapshot, False, "Take a picture with the bench camera and look at it.",
     _schema({})),
    ("wait", wait, False, "Wait 1..120 seconds, e.g. for the DUT to boot before reading its console.",
     _schema({"seconds": _N}, ["seconds"])),
    # approval for shell follows its own switch (agent.py), not the DUT-actions one
    ("shell", shell, True, "Run a Linux command on the Pi with bash, as the user m4013pi in its home "
     "directory (passwordless sudo works). No terminal and no input: use non-interactive flags "
     "(e.g. apt-get -y, systemctl --no-pager). Returns the exit code and output (stdout+stderr).",
     _schema({"command": {"type": "string"},
              "timeout_s": {**_N, "description": "1..600, default 60"}}, ["command"])),
]
HANDLERS = {n: (fn, writes) for n, fn, writes, _, _ in TOOLS}
SCHEMAS = [{"name": n, "description": d, "input_schema": s} for n, _, _, d, s in TOOLS]


def needs_approval(name, args):
    w = HANDLERS[name][1]
    return w(args) if callable(w) else w


async def run(name, args):
    """-> (content, is_error); content is a string or a list of blocks."""
    try:
        out = await HANDLERS[name][0](args or {})
    except (ValueError, TypeError, OSError, ConnectionError) as e:
        return f"error: {e}", True
    return (_clip(out) if isinstance(out, str) else out), False
