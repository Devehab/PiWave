#!/bin/bash
# Self-signed HTTPS certificate so the browser allows the microphone (voice recording).
# usage: make-cert.sh [--if-needed]
set -eu
DIR=/etc/piradio
CERT=$DIR/cert.pem
KEY=$DIR/key.pem
IP=${PIRADIO_AP_IP:-192.168.4.1}
if [ "${1:-}" = "--if-needed" ] && [ -s "$CERT" ] && [ -s "$KEY" ] \
   && openssl x509 -checkend 2592000 -noout -in "$CERT" >/dev/null 2>&1; then
  exit 0   # still valid for more than 30 days
fi
mkdir -p "$DIR"
HOST=$(hostname)
# Apple devices reject certificates valid for more than 825 days.
openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 825 \
  -subj "/CN=PiRadio" \
  -addext "subjectAltName=IP:$IP,DNS:$HOST.local,DNS:piradio.local" \
  -addext "extendedKeyUsage=serverAuth" \
  -addext "keyUsage=digitalSignature,keyEncipherment" \
  -keyout "$KEY.tmp" -out "$CERT.tmp" >/dev/null 2>&1
chmod 600 "$KEY.tmp"
mv -f "$KEY.tmp" "$KEY"
mv -f "$CERT.tmp" "$CERT"
echo "certificate created: $CERT"
