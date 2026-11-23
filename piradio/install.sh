#!/bin/bash
# =====================================================================
#  PiRadio installer
#  Run on the Raspberry Pi (needs internet once, for ffmpeg + Bluetooth):
#      sudo bash /boot/firmware/piradio/install.sh
#  (or from wherever the folder is, e.g.  sudo bash ~/piradio/install.sh)
#  Safe to run again: it repairs/upgrades an existing installation.
# =====================================================================
set -Eeuo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE=/opt/piradio
DATA=/var/lib/piradio
LIB=/usr/local/lib/piradio
HOT=PiRadio-Hotspot
AP_IP=192.168.4.1
BOOTDIR=/boot/firmware; [ -d "$BOOTDIR" ] || BOOTDIR=/boot

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run with sudo:  sudo bash $0"
  exit 1
fi

# --- log everything, also onto the SD card's boot partition (readable on a computer)
LOG=/var/log/piradio-install.log
exec > >(tee -a "$LOG") 2>&1
trap 'echo; echo "!!! INSTALL FAILED at line $LINENO. See $LOG (copy: $BOOTDIR/piradio-install.log)"; cp -f "$LOG" "$BOOTDIR/piradio-install.log" 2>/dev/null || true' ERR

VERSION=$(cat "$DIR/VERSION")
step() { echo; echo "==> [$1/10] $2"; }
echo "=================================================="
echo "  PiRadio $VERSION installer   $(date)"
echo "=================================================="

# --- settings from piradio.conf (strip Windows line endings just in case)
WIFI_NAME="PiRadio"; WIFI_PASSWORD="piradio123"; WIFI_COUNTRY="JO"
FREQUENCY="107.9"; STATION_NAME="PI RADIO"; CAPTIVE_PORTAL="yes"
if [ -f "$DIR/piradio.conf" ]; then
  # shellcheck disable=SC1090
  source <(tr -d '\r' < "$DIR/piradio.conf")
fi
if [ ${#WIFI_PASSWORD} -lt 8 ]; then
  echo "WIFI_PASSWORD in piradio.conf must be at least 8 characters"; exit 1
fi

step 1 "Checking the system"
MODEL=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown)
. /etc/os-release
echo "board: $MODEL"
echo "os:    $PRETTY_NAME ($(uname -m))"
case "$MODEL" in
  *"Pi 5"*|*"Pi 500"*) echo "The Raspberry Pi 5 cannot transmit FM this way. Use a Zero 2 W / 3 / 4."; exit 1 ;;
esac
command -v nmcli >/dev/null || { echo "NetworkManager is required (Raspberry Pi OS Bookworm or newer)."; exit 1; }

step 2 "Installing packages (ffmpeg etc.) - this can take 5-15 minutes on a Zero 2 W"
if ! getent hosts deb.debian.org >/dev/null && ! getent hosts archive.raspberrypi.com >/dev/null; then
  echo "No internet. Connect the Pi to your home Wi-Fi first (Raspberry Pi Imager settings),"
  echo "or run:  sudo piradio-wifi home   (if PiRadio was installed before)."
  exit 1
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  ffmpeg python3 python3-flask libsndfile1-dev gcc libc6-dev openssl \
  dnsmasq-base avahi-daemon iw rfkill
# Bluetooth speaker mode (optional: the radio works without it)
apt-get install -y --no-install-recommends \
  bluez bluez-tools bluez-alsa-utils libasound2-plugin-bluez \
  || echo "WARNING: Bluetooth packages could not be installed; FM radio still works."

step 3 "Creating folders"
mkdir -p "$BASE/releases" "$DATA/music" "$DATA/tmp" /etc/piradio "$LIB"
if [ ! -f "$DATA/settings.json" ]; then
  python3 - "$DATA/settings.json" "$FREQUENCY" "$STATION_NAME" <<'EOF'
import json, sys
path, freq, ps = sys.argv[1], float(sys.argv[2]), sys.argv[3][:8]
json.dump({"frequency": freq, "station_name": ps}, open(path, "w"))
EOF
  echo "first settings: $FREQUENCY MHz, station \"$STATION_NAME\""
fi

step 4 "Installing PiRadio $VERSION and compiling the FM transmitter"
systemctl stop piradio 2>/dev/null || true
PIRADIO_BASE=$BASE PIRADIO_DATA=$DATA python3 "$DIR/app/updater.py" deploy "$DIR" --no-pending
REL=$(readlink -f "$BASE/current")
cp -f "$DIR/piradio.conf" "$REL/piradio.conf" 2>/dev/null || true

step 5 "Installing the service"
install -m 755 "$REL/app/updater.py" "$LIB/updater.py"
install -m 755 "$REL/system/make-cert.sh" "$LIB/make-cert.sh"
install -m 755 "$REL/system/setup-bluetooth.sh" "$LIB/setup-bluetooth.sh"
install -m 755 "$REL/system/piradio-launch" /usr/local/sbin/piradio-launch
install -m 755 "$REL/system/piradio-wifi" /usr/local/sbin/piradio-wifi
install -m 644 "$REL/system/piradio.service" /etc/systemd/system/piradio.service
# keep logs in RAM: fewer SD-card writes when the car switches power off
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nStorage=volatile\nRuntimeMaxUse=24M\n' > /etc/systemd/journald.conf.d/piradio.conf
systemctl daemon-reload
systemctl enable piradio

step 6 "HTTPS certificate (needed for voice recording)"
PIRADIO_AP_IP=$AP_IP "$LIB/make-cert.sh"

step 7 "Wi-Fi hotspot \"$WIFI_NAME\""
rfkill unblock wifi || true
if command -v raspi-config >/dev/null && [ -z "$(raspi-config nonint get_wifi_country 2>/dev/null || true)" ]; then
  raspi-config nonint do_wifi_country "$WIFI_COUNTRY" || true
fi
nmcli connection delete "$HOT" >/dev/null 2>&1 || true
# autoconnect stays off until the end so this SSH session is not cut
nmcli connection add type wifi ifname wlan0 con-name "$HOT" autoconnect no ssid "$WIFI_NAME" \
  802-11-wireless.mode ap 802-11-wireless.band bg 802-11-wireless.channel 6 \
  802-11-wireless.powersave 2 \
  ipv4.method shared ipv4.addresses "$AP_IP/24" ipv6.method disabled \
  wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$WIFI_PASSWORD" \
  wifi-sec.proto rsn wifi-sec.pairwise ccmp wifi-sec.group ccmp wifi-sec.pmf disable \
  connection.autoconnect-priority 100 >/dev/null
mkdir -p /etc/NetworkManager/dnsmasq-shared.d
if [ "$CAPTIVE_PORTAL" = "yes" ]; then
  # every web address -> the radio page, so phones open it automatically
  printf '# PiRadio captive portal\naddress=/#/%s\n' "$AP_IP" > /etc/NetworkManager/dnsmasq-shared.d/piradio.conf
else
  printf '# PiRadio\naddress=/piradio.local/%s\n' "$AP_IP" > /etc/NetworkManager/dnsmasq-shared.d/piradio.conf
fi
echo "hotspot created (it starts after the reboot)"

step 8 "Bluetooth speaker \"PiRadio\""
bash "$REL/system/setup-bluetooth.sh" \
  || echo "WARNING: Bluetooth setup failed; FM radio still works. Retry later from the web page."

step 9 "Music folder on the SD card"
mkdir -p "$BOOTDIR/piradio-music"
cat > "$BOOTDIR/piradio-music/README.txt" <<'EOF'
Put MP3 / M4A / WAV / FLAC songs in this folder from your computer.
On the next start of the Raspberry Pi they are moved into the PiRadio library.
ضع الأغاني بهذا المجلد من الكمبيوتر، وعند تشغيل الريسبيري تنتقل تلقائياً لمكتبة الراديو.
EOF

step 10 "Done"
nmcli connection modify "$HOT" connection.autoconnect yes
cp -f "$LOG" "$BOOTDIR/piradio-install.log" 2>/dev/null || true
cat <<EOF

==================================================
  PiRadio $VERSION installed successfully
==================================================
 After the reboot (about 1 minute):
   Wi-Fi name : $WIFI_NAME
   Password   : $WIFI_PASSWORD
   Web page   : http://$AP_IP
   FM         : $FREQUENCY MHz  (car radio -> FM -> $FREQUENCY)
 Bluetooth  : "PiRadio" (pairing mode: web page -> Bluetooth)
 Antenna: one ~20 cm wire on pin 7 (GPIO4) only.
==================================================
EOF
if [ "${PIRADIO_NO_REBOOT:-}" != "1" ]; then
  read -r -t 30 -p "Reboot now? [Y/n] (auto-yes in 30 s) " ans || ans=y
  case "${ans:-y}" in
    n|N) echo "Reboot later with: sudo reboot" ;;
    *) echo "Rebooting... the SSH connection will close."; sleep 2; reboot ;;
  esac
fi
