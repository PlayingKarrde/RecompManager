# UltraTears: build the Linux / Steam Deck game from the user's exefs/romfs dump.
# tools/setup.py fetches its own toolchain (python-build-standalone, LLVM, the
# Linux kit: about 3 GB), so the build needs the network.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git python3 ca-certificates curl xz-utils zstd tar file \
 && rm -rf /var/lib/apt/lists/*

COPY build.sh /builder/build.sh
RUN chmod 755 /builder/build.sh
