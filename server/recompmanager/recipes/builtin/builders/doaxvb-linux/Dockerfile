# doaxvb-re: build the native Linux runner from the user's USA Xbox ISO.
# Packages from docs/building.md ("Linux and SteamOS build", Ubuntu 24.04).
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      cmake ninja-build clang git python3 python3-venv python3-pip build-essential \
      pkg-config unzip ca-certificates curl \
      libx11-dev libxext-dev libxrandr-dev libxcursor-dev libxfixes-dev libxi-dev \
      libxss-dev libxtst-dev libxkbcommon-dev libwayland-dev libdecor-0-dev \
      libasound2-dev libpulse-dev libvulkan-dev libshaderc1 \
 && rm -rf /var/lib/apt/lists/*

COPY build.sh /builder/build.sh
RUN chmod 755 /builder/build.sh
