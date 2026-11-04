"""HTTPS client to the DUT's BMC. Self-signed certificates are accepted."""

import aiohttp

from . import config, settings

METHODS = {"GET", "POST", "PATCH", "PUT", "DELETE"}


async def request(method, path, body):
    """Returns {"status", "body"} plus "error" when the request never completed."""
    s = settings.get()
    method = (method or "GET").upper()
    if method not in METHODS or not path or not path.startswith("/"):
        return {"status": 0, "error": "bad method or path", "body": ""}

    url = f"{'https' if s['dut_https'] else 'http'}://{s['dut_host']}{path}"
    auth = aiohttp.BasicAuth(s["dut_user"], s["dut_pass"]) if s["dut_user"] else None
    headers = {"Accept": "application/json"}
    if body:
        headers["Content-Type"] = "application/json"
    timeout = aiohttp.ClientTimeout(total=config.REDFISH_TIMEOUT_S)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.request(method, url, data=body or None, headers=headers,
                                    auth=auth, ssl=False) as r:
                data = await r.content.read(config.REDFISH_MAX_BODY)
                print(f"redfish: {method} {path} -> {r.status} ({len(data)} bytes)", flush=True)
                return {"status": r.status, "body": data.decode("utf-8", "replace")}
    except TimeoutError:
        return {"status": 0, "error": f"no response from {s['dut_host'][:60]}", "body": ""}
    except aiohttp.ClientError as e:
        return {"status": 0, "error": f"cannot reach {s['dut_host'][:40]} ({type(e).__name__})", "body": ""}
