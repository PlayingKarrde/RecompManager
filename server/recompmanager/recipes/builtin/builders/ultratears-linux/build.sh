#!/bin/sh
# Inputs: $RELEASE (UltraTears.AppImage), $INPUT_INPUT (folder with exefs/ and romfs/),
# $VERSION (release tag, e.g. v0.3.1). Output: $OUTPUT/UltraTears-<version>-linux-x64.tar.
#
# Runs every step of tools/setup.py except "graphics": that one compiles shaders with
# the local GPU driver, which a server doesn't have. On the Deck, "Prepare the game"
# in the launcher finds everything else up to date and only does that step (1-3 min).
set -eu

cd "$WORK"
if [ ! -d src/.git ]; then
  git clone --depth 1 --branch "$VERSION" https://github.com/kuraeow/ultratears.git src
fi
cd src

for step in romfs lift compile shaders textures finish; do
  echo "== UltraTears setup step: $step"
  python3 tools/setup.py --dump "$INPUT_INPUT" --output "$WORK/game" --only "$step"
done

cp "$RELEASE" "$WORK/game/UltraTears.AppImage"
chmod 755 "$WORK/game/UltraTears.AppImage"

echo "Packing the game folder (this is large)"
tar -cf "$OUTPUT/UltraTears-$VERSION-linux-x64.tar" -C "$WORK/game" .
rm -rf "$WORK/game"
