#!/bin/bash
# Compile the PiFmRds FM transmitter for the Raspberry Pi model this runs on.
# usage: build-pifmrds.sh <source-dir> <output-binary>
set -euo pipefail
SRC="$1"
OUT="$2"

MODEL=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo "unknown")
case "$MODEL" in
  *"Pi 5"*|*"Pi 500"*|*"Compute Module 5"*)
    echo "ERROR: $MODEL is not supported (the Pi 5 cannot generate FM on GPIO4)."
    exit 1 ;;
  *"Pi 4"*|*"Pi 400"*|*"Compute Module 4"*) RASPI=4 ;;
  *"Zero 2"*|*"Pi 3"*|*"Pi 2"*|*"Compute Module 3"*) RASPI=2 ;;
  *"Zero"*|*"Model A"*|*"Model B"*|*"Compute Module Rev"*) RASPI=1 ;;
  *) echo "WARNING: unknown model '$MODEL', using the Pi 2/3/Zero 2 layout"; RASPI=2 ;;
esac
echo "model: $MODEL -> RASPI=$RASPI, arch: $(uname -m), gcc: $(gcc -dumpversion)"

FILES=(rds.c rds_strings.c waveforms.c mailbox.c pi_fm_rds.c fm_mpx.c control_pipe.c)
mkdir -p "$(dirname "$OUT")"
cd "$SRC"
build() {
  # shellcheck disable=SC2068
  gcc -std=gnu99 -Wall -Wno-unused-result $@ -DRASPI=$RASPI -o "$OUT.tmp" "${FILES[@]}" -lsndfile -lm
}
if build -O3 -ffast-math -mcpu=native 2>/tmp/piradio-build.err; then
  echo "built with -O3 -mcpu=native"
elif build -O2 -ffast-math 2>/tmp/piradio-build.err; then
  echo "built with -O2"
else
  cat /tmp/piradio-build.err
  exit 1
fi

# sanity check: the program must start and reject an invalid frequency
if ! "$OUT.tmp" -freq 999 2>&1 | grep -q "Incorrect frequency"; then
  echo "ERROR: the compiled program does not run correctly"
  exit 1
fi
mv -f "$OUT.tmp" "$OUT"
chmod 755 "$OUT"
echo "OK: $OUT"
