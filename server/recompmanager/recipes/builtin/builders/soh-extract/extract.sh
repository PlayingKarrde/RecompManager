#!/bin/sh
# Inputs: $RELEASE (SoH-<codename>.appimage, or an older -Linux.zip), $INPUT_OOT and/or
# $INPUT_OOT_MQ (ROM files). Output: $OUTPUT/oot.o2r and/or $OUTPUT/oot-mq.o2r.
set -eu

cd "$WORK"
mkdir -p tmp home app
export TMPDIR="$WORK/tmp"
export SHIP_HOME="$WORK/home"

case "$RELEASE" in
  *.zip)
    unzip -q -o "$RELEASE" -d app
    APP=$(find app -iname '*.appimage' | head -n 1)
    ;;
  *)
    cp "$RELEASE" app/soh.appimage
    APP=app/soh.appimage
    ;;
esac
[ -n "$APP" ] || { echo "No AppImage in the release"; exit 1; }
chmod +x "$APP"

roms=""
expect=""
if [ -n "${INPUT_OOT:-}" ]; then roms="$roms $INPUT_OOT"; expect="$expect oot.o2r"; fi
if [ -n "${INPUT_OOT_MQ:-}" ]; then roms="$roms $INPUT_OOT_MQ"; expect="$expect oot-mq.o2r"; fi
[ -n "$roms" ] || { echo "No ROM given"; exit 1; }

echo "Starting Ship of Harkinian to extract:$expect"
# shellcheck disable=SC2086
xvfb-run -a -s "-screen 0 1280x720x24" "$APP" $roms > "$WORK/soh.log" 2>&1 &
pid=$!

# Done when every expected archive exists and its size has held still for 15 s.
deadline=$(( $(date +%s) + 1500 ))
last=""
stable=0
while :; do
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "Ship of Harkinian exited early. Its output:"; tail -n 40 "$WORK/soh.log"; exit 1
  fi
  sizes=""
  missing=0
  for f in $expect; do
    if [ -f "$SHIP_HOME/$f" ]; then
      sizes="$sizes $(stat -c %s "$SHIP_HOME/$f")"
    else
      missing=1
    fi
  done
  if [ "$missing" = 0 ] && [ "$sizes" = "$last" ]; then
    stable=$((stable + 5))
    [ "$stable" -ge 15 ] && break
  else
    stable=0
  fi
  last="$sizes"
  if [ "$(date +%s)" -gt "$deadline" ]; then
    echo "Timed out waiting for:$expect"; tail -n 40 "$WORK/soh.log"; kill "$pid"; exit 1
  fi
  sleep 5
done

kill "$pid" 2>/dev/null || true
pkill -f Xvfb 2>/dev/null || true
for f in $expect; do
  cp "$SHIP_HOME/$f" "$OUTPUT/$f"
  echo "Made $f ($(stat -c %s "$OUTPUT/$f") bytes)"
done
