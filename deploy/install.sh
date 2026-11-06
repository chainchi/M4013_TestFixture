#!/bin/sh
# Installs or updates the Pi Test Host. Run on the Pi from the project folder:
#   sudo sh deploy/install.sh
set -e
cd "$(dirname "$0")/.."

apt-get install -y -qq -o DPkg::Lock::Timeout=600 \
    python3-aiohttp python3-libgpiod python3-smbus2 python3-serial python3-pil python3-numpy python3-venv     gpiod i2c-tools avahi-daemon iw

# Venv for the agent's Claude SDK (Ubuntu's python3-anthropic is too old); it
# still sees the apt packages above
V=/opt/pi-tester-venv
[ -x $V/bin/python ] || python3 -m venv --system-site-packages $V
$V/bin/pip install -q -U anthropic

# Second I2C bus (GPIO 4/5) and 2 hardware PWM channels (GPIO 18/19); I2C bus 0 is i2c_arm
C=/boot/firmware/config.txt
REBOOT=
if ! grep -q 'pi-tester' $C; then
    cp -n $C $C.orig
    printf '\n[all]\n# pi-tester: second I2C bus (GPIO 4/5) and 2 hardware PWM (GPIO 18/19)\ndtoverlay=i2c2-pi5,pins_4_5\ndtoverlay=pwm-2chan,pin=18,func=2,pin2=19,func2=2\n' >> $C
    REBOOT=1
fi
grep -q '^dtparam=i2c_arm=on' $C || { echo 'dtparam=i2c_arm=on' >> $C; REBOOT=1; }

rm -rf /opt/pi-tester
mkdir -p /opt/pi-tester /var/lib/pi-tester
cp -r pi_tester /opt/pi-tester/
find /opt/pi-tester -name __pycache__ -prune -exec rm -rf {} +

# First install: start from the network the card was flashed with
if [ ! -f /var/lib/pi-tester/settings.json ]; then
    python3 - <<'EOF'
import glob, ipaddress, json, os, yaml
s = {}
for f in sorted(glob.glob("/etc/netplan/*.yaml")):
    net = (yaml.safe_load(open(f)) or {}).get("network", {})
    for addr in net.get("ethernets", {}).get("eth0", {}).get("addresses", []):
        i = ipaddress.IPv4Interface(addr)
        s.update(static_ip=True, ip=str(i.ip), netmask=str(i.netmask))
    for ssid, ap in net.get("wifis", {}).get("wlan0", {}).get("access-points", {}).items():
        ap = ap or {}
        s.update(wifi_ssid=ssid, wifi_pass=ap.get("password") or ap.get("auth", {}).get("password", ""))
path = "/var/lib/pi-tester/settings.json"
with open(path, "w") as f:
    json.dump(s, f, indent=2)
os.chmod(path, 0o600)
print("settings seeded:", {k: v for k, v in s.items() if k != "wifi_pass"})
EOF
fi

cp deploy/pi-tester.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable pi-tester >/dev/null 2>&1
systemctl restart pi-tester
echo "pi-tester installed and started"
[ -z "$REBOOT" ] || echo "config.txt changed: reboot to enable the I2C bus 1 and PWM pins"
