#!/bin/bash
# Make the Raspberry Pi a Bluetooth speaker for PiRadio (A2DP sink).
#   setup-bluetooth.sh --install   install the packages first (needs internet), then configure
#   setup-bluetooth.sh             configure only (works offline, safe to run again)
# Exit codes: 0 ok, 2 packages missing, 3 no internet.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKGS="bluez bluez-tools bluez-alsa-utils libasound2-plugin-bluez"

if [ "${1:-}" = "--install" ]; then
  if ! getent hosts deb.debian.org >/dev/null && ! getent hosts archive.raspberrypi.com >/dev/null; then
    echo "No internet: connect the Pi to your home Wi-Fi first."
    exit 3
  fi
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  # shellcheck disable=SC2086
  apt-get install -y --no-install-recommends $PKGS
fi

for c in bluetoothctl bluealsa bt-agent; do
  command -v "$c" >/dev/null || { echo "Bluetooth packages are missing ($c). Run: sudo $0 --install"; exit 2; }
done

# 1) bluetoothd: look like a loudspeaker, never discoverable/pairable unless
#    PiRadio switches pairing mode on (and BlueZ turns it off after 3 minutes)
python3 - /etc/bluetooth/main.conf <<'PY'
import os, re, sys
path = sys.argv[1]
want = {"General": {"Class": "0x200414", "DiscoverableTimeout": "180", "PairableTimeout": "180",
                    "JustWorksRepairing": "always", "FastConnectable": "true"},
        "Policy": {"AutoEnable": "true"}}
try:
    lines = open(path).read().splitlines()
except OSError:
    lines = []
out, section, done = [], None, set()
def flush(sec):
    for k, val in want.get(sec, {}).items():
        if (sec, k) not in done:
            out.append("%s = %s" % (k, val)); done.add((sec, k))
for line in lines:
    m = re.match(r"^\s*\[(\w+)\]", line)
    if m:
        flush(section); section = m.group(1); out.append(line); continue
    m = re.match(r"^\s*#?\s*(\w+)\s*=", line)
    if m and section in want and m.group(1) in want[section]:
        k = m.group(1)
        if (section, k) not in done:
            out.append("%s = %s" % (k, want[section][k])); done.add((section, k))
        continue
    out.append(line)
flush(section)
for sec in want:
    missing = [k for k in want[sec] if (sec, k) not in done]
    if missing:
        out.append(""); out.append("[%s]" % sec)
        for k in missing:
            out.append("%s = %s" % (k, want[sec][k])); done.add((sec, k))
if os.path.exists(path) and not os.path.exists(path + ".piradio-orig"):
    os.rename(path, path + ".piradio-orig")
open(path, "w").write("\n".join(out) + "\n")
PY

# 2) BlueALSA: only receive audio from phones (A2DP sink = "speaker")
mkdir -p /etc/systemd/system/bluealsa.service.d
cat > /etc/systemd/system/bluealsa.service.d/piradio.conf <<'UNIT'
[Service]
ExecStart=
ExecStart=/usr/bin/bluealsa -S --keep-alive=5 -p a2dp-sink
UNIT
# PiRadio reads the phone's audio itself: the default player must not grab it
systemctl disable --now bluealsa-aplay.service >/dev/null 2>&1 || true
systemctl mask bluealsa-aplay.service >/dev/null 2>&1 || true

# 3) pairing agent without PIN or screen (only works while pairing mode is on)
install -m 644 "$DIR/piradio-bt-agent.service" /etc/systemd/system/piradio-bt-agent.service

rfkill unblock bluetooth 2>/dev/null || true
systemctl daemon-reload
systemctl enable bluetooth bluealsa piradio-bt-agent >/dev/null 2>&1 || true
systemctl restart bluetooth
sleep 2
systemctl restart bluealsa piradio-bt-agent || true
echo "Bluetooth ready: phones can pair with PiRadio (turn on pairing mode from the web page)."
