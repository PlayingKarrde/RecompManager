#!/bin/sh
# Inputs: $RELEASE (DOAXBV-<version>-Linux.zip), $INPUT_INPUT (the ISO).
# Output: $OUTPUT/doaxvb-<version>-linux-x64.tar, the built game folder.
set -eu

cd "$WORK"
rm -rf src
mkdir src
unzip -q "$RELEASE" -d src
top=$(find src -mindepth 1 -maxdepth 1)
if [ "$(echo "$top" | wc -l)" = 1 ] && [ -d "$top" ]; then GAME="$top"; else GAME=src; fi
cd "$GAME"
chmod +x ./*.sh

echo "Building doaxvb-re $VERSION for Linux from the ISO"
./build_game.sh "$INPUT_INPUT" < /dev/null

echo "Packing the game folder"
tar -cf "$OUTPUT/doaxvb-$VERSION-linux-x64.tar" -C "$WORK/$GAME" .
