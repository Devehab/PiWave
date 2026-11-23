#!/bin/bash
# Runs during an update from the web page (offline), before the new version
# is switched on. Refreshes the helper scripts and the service file. If the
# Bluetooth packages are already on the Pi, it also (re)configures them;
# otherwise the page offers to install them once the Pi has internet.
set -euo pipefail
NEW="${PIRADIO_NEW:?}"
LIB=/usr/local/lib/piradio
[ "$(id -u)" -eq 0 ] || { echo "post-update: not root, skipping system files"; exit 0; }
mkdir -p "$LIB"
install -m 755 "$NEW/app/updater.py" "$LIB/updater.py"
install -m 755 "$NEW/system/make-cert.sh" "$LIB/make-cert.sh"
install -m 755 "$NEW/system/setup-bluetooth.sh" "$LIB/setup-bluetooth.sh"
install -m 755 "$NEW/system/piradio-launch" /usr/local/sbin/piradio-launch
install -m 755 "$NEW/system/piradio-wifi" /usr/local/sbin/piradio-wifi
install -m 644 "$NEW/system/piradio.service" /etc/systemd/system/piradio.service
install -m 644 "$NEW/system/piradio-bt-agent.service" /etc/systemd/system/piradio-bt-agent.service
systemctl daemon-reload || true
if command -v bluealsa >/dev/null && command -v bt-agent >/dev/null; then
  bash "$NEW/system/setup-bluetooth.sh" || echo "post-update: Bluetooth setup failed (radio still works)"
else
  echo "post-update: Bluetooth packages not installed yet (install them from the page)"
fi
echo "post-update: done"
