# Pi Test Host: HTTP API

Every control on the web page is also a plain HTTP call, so the tester can be
driven from scripts, curl, PowerShell, Postman or a test framework.

- **Base URL:** `http://<tester-ip>/`, e.g. `http://192.168.10.2/` on the bench
  hotspot. Also reachable as `http://m4013pi.local/` where mDNS works.
- **Format:** requests and replies are JSON. Send `Content-Type: application/json`
  with every POST.
- **Result:** write calls answer `{"ok":true}`, or `{"ok":false,"error":"..."}`.
- **No login.** Anyone on the same network can drive the DUT pins, so keep the
  tester on an isolated station network.

This is the tester's own REST API. It is *not* Redfish; Redfish is what the
tester speaks to the DUT (see `/api/redfish` below).

## Endpoints

| Method | Path | Body | Does |
|---|---|---|---|
| GET  | `/api/status` | – | Everything: GPIO, PWM, UART, network |
| POST | `/api/gpio` | `{"ch":0,"level":1}` | Set GPIO output `ch` (0–1) high (1) or low (0) |
| POST | `/api/pwm` | `{"ch":0,"enable":true,"freq":25000,"duty":50}` | Set PWM `ch` (0–1); omitted fields keep their value |
| POST | `/api/i2c/scan` | `{"bus":0}` | List 7-bit addresses that ACK on bus 0 or 1 |
| POST | `/api/i2c/xfer` | `{"bus":0,"addr":64,"write":"88","read":3}` | Write bytes, then read N bytes (repeated start) |
| POST | `/api/uart` | `{"port":0,"baud":115200}` or `{"port":0,"clear":true}` | Set UART baud / clear its scrollback |
| GET  | `/api/uart/log?port=0` | – | Download UART scrollback as text |
| GET  | `/api/uart/stream?port=0[&history=1]` | – | Live UART output, never ends (`curl -N`); max 4 at once |
| POST | `/api/redfish` | `{"method":"GET","path":"/redfish/v1/Systems","body":""}` | Send a Redfish request to the DUT, returns `{"status":200,"body":"..."}` |
| GET/POST | `/api/settings` | see web page | Station settings (IP, DUT host, credentials, Wi-Fi) |
| WS   | `/ws` | binary `[port][bytes]` | Live UART consoles (used by the web page) |

### Pin map
| Signal | Pi GPIO | Header pin | API |
|---|---|---|---|
| OUT0 / OUT1 | 17 / 27 | 11 / 13 | `/api/gpio` `ch` 0 / 1 |
| IN0 / IN1 | 22 / 23 | 15 / 16 | `gpio_in[0]` / `[1]` in `/api/status` |
| PWM0 / PWM1 | 18 / 19 | 12 / 35 | `/api/pwm` `ch` 0 / 1 |
| I2C bus 0 SDA / SCL | 2 / 3 | 3 / 5 | `"bus":0` |
| I2C bus 1 SDA / SCL | 4 / 5 | 7 / 29 | `"bus":1` |

### Differences from the P4 API
- **I2C `speed` is ignored**: Linux fixes each bus's clock at boot. Scan replies
  carry the bus `speed`, and a transfer that asks for a different speed gets a
  `"note"` in its reply.
- **PWM `res_bits`** comes from the Pi's 50 MHz PWM clock (25 kHz → 10-bit).
- **`/api/status`** also gives each UART's device path in `uart[i].dev`.

### Field notes
- **GPIO:** `/api/status` returns `gpio_out` (read back from the pin) and `gpio_in`, e.g. `"gpio_in":[1,0]`.
- **PWM:** `freq` in Hz, 1 Hz – 1 MHz. `duty` in percent, 0–100, decimals allowed.
  `enable:false` holds the pin low. Status also reports `res_bits`, the duty
  resolution available at that frequency (25 kHz → 10-bit).
- **I2C:** `addr` is the 7-bit address as a number (`64` = `0x40`). `write` is hex
  bytes as text (`"88"`, `"00 10"`, `"0x00,0x10"`). `read` is the number of bytes
  (0–256). Reply: `{"ok":true,"data":"98 F3 E8"}`, or `"error":"NACK / no device"`.
  The scan reply also gives the idle `sda`/`scl` levels (1 = OK, 0 = line held low).
  The scan probes each address with a 1-byte read and lists it only if it
  answers 3 times in a row (address-only probes gave false hits on some targets).
  Only one master per bus: another controller on the same wires corrupts scans.

## Examples

### curl (Windows cmd uses `curl.exe` and `\"` inside the JSON)
```bat
set H=http://192.168.10.2

curl %H%/api/status

:: GPIO OUT0 high, then low
curl -X POST -H "Content-Type: application/json" -d "{\"ch\":0,\"level\":1}" %H%/api/gpio
curl -X POST -H "Content-Type: application/json" -d "{\"ch\":0,\"level\":0}" %H%/api/gpio

:: PWM0: 25 kHz, 40 %, on;  then off
curl -X POST -H "Content-Type: application/json" -d "{\"ch\":0,\"enable\":true,\"freq\":25000,\"duty\":40}" %H%/api/pwm
curl -X POST -H "Content-Type: application/json" -d "{\"ch\":0,\"enable\":false}" %H%/api/pwm

:: I2C: scan bus 0, then read 3 bytes of register 0x88 at 0x40
curl -X POST -H "Content-Type: application/json" -d "{\"bus\":0}" %H%/api/i2c/scan
curl -X POST -H "Content-Type: application/json" -d "{\"bus\":0,\"addr\":64,\"write\":\"88\",\"read\":3}" %H%/api/i2c/xfer
```
On Linux/macOS/Git Bash use single quotes instead: `-d '{"ch":0,"level":1}'`.

### PowerShell
```powershell
$H = "http://192.168.10.2"
function Post($path, $body) {
    Invoke-RestMethod -Method Post -Uri "$H$path" -ContentType "application/json" -Body ($body | ConvertTo-Json -Compress)
}

Invoke-RestMethod "$H/api/status"                         # read everything
(Invoke-RestMethod "$H/api/status").gpio_in               # just the inputs

Post "/api/gpio" @{ ch = 0; level = 1 }                   # OUT0 high
Post "/api/pwm"  @{ ch = 1; enable = $true; freq = 1000; duty = 75 }
Post "/api/i2c/scan" @{ bus = 0 }
Post "/api/i2c/xfer" @{ bus = 0; addr = 0x40; write = "88"; read = 3 }
```

### Python (requests)
```python
import requests

H = "http://192.168.10.2"

def post(path, **body):
    r = requests.post(H + path, json=body, timeout=15)
    r.raise_for_status()
    return r.json()

print(requests.get(H + "/api/status", timeout=5).json()["gpio_in"])
post("/api/gpio", ch=0, level=1)
post("/api/pwm", ch=0, enable=True, freq=25000, duty=40)
print(post("/api/i2c/scan", bus=0)["found"])                 # e.g. [64]
print(post("/api/i2c/xfer", bus=0, addr=0x40, write="88", read=3)["data"])

# loopback check: OUT0 wired to IN0
for level in (1, 0):
    post("/api/gpio", ch=0, level=level)
    assert requests.get(H + "/api/status").json()["gpio_in"][0] == level
```
