# Ship of Harkinian: generate oot.o2r / oot-mq.o2r from the user's ROM(s).
#
# SoH has no headless extractor, but on Linux ROM paths given on the command line
# are extracted straight away (OTRGlobals::RunExtract, ES_EXTRACT_ARGS). So the
# AppImage runs under a virtual X display, writes the archives into SHIP_HOME,
# and is stopped once they're complete.
FROM debian:bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      xvfb xauth procps ca-certificates unzip \
      libgl1 libegl1 libgl1-mesa-dri libglx-mesa0 libgles2 \
      libx11-6 libxext6 libxrandr2 libxi6 libxcursor1 libxinerama1 libxss1 libxkbcommon0 \
      libasound2 libpulse0 libfontconfig1 libfreetype6 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

# AppImages need FUSE to mount themselves; inside a container they unpack instead.
ENV APPIMAGE_EXTRACT_AND_RUN=1 \
    LIBGL_ALWAYS_SOFTWARE=1 \
    SDL_AUDIODRIVER=dummy

COPY extract.sh /builder/extract.sh
RUN chmod 755 /builder/extract.sh
