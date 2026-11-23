#!/bin/bash
# Build release files from ../piradio
#   dev/make-update.sh            -> refresh piradio/MANIFEST.sha256 (SD-card folder)
#                                    + dist/piradio-update-<VERSION>.tar.gz
#   dev/make-update.sh 1.0.1      -> same, but stamps VERSION=1.0.1 first
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKG="$ROOT/piradio"
if [ -n "${1:-}" ]; then echo "$1" > "$PKG/VERSION"; fi
VERSION=$(cat "$PKG/VERSION")

# clean junk that macOS / Python leave behind
find "$PKG" \( -name '__pycache__' -o -name '.DS_Store' -o -name '._*' \) -prune -exec rm -rf {} +

python3 "$PKG/app/updater.py" manifest "$PKG"
mkdir -p "$ROOT/dist"
OUT="$ROOT/dist/piradio-update-$VERSION.tar.gz"
# COPYFILE_DISABLE stops macOS tar from adding ._ files
COPYFILE_DISABLE=1 tar -C "$ROOT" --exclude='__pycache__' --exclude='.DS_Store' --exclude='._*' \
  -czf "$OUT" piradio
echo "SD-card folder : $PKG  (version $VERSION)"
echo "update package : $OUT ($(du -h "$OUT" | cut -f1))"
