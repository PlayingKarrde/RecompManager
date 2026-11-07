# syntax=docker/dockerfile:1.7

# ---- Web UI ----
FROM node:24-alpine AS web
WORKDIR /src/web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
COPY docs/openapi.json /src/docs/openapi.json
RUN npm run build

# ---- Python wheel ----
FROM python:3.12-slim-trixie AS wheel
WORKDIR /src
COPY server/ ./
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

# ---- Runtime ----
FROM python:3.12-slim-trixie
ARG VERSION=dev
LABEL org.opencontainers.image.title="Recomp Manager" \
      org.opencontainers.image.source="https://github.com/PlayingKarrde/RecompManager" \
      org.opencontainers.image.version="${VERSION}"

# 7zip: .7z release assets. tzdata: TZ env. setpriv (util-linux) is in the base image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends 7zip tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && 7z i > /dev/null && command -v setpriv

COPY --from=wheel /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels
COPY --from=web /src/web/dist /app/web
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh

# Unraid defaults: nobody:users
ENV PUID=99 PGID=100 UMASK=022 TZ=Etc/UTC \
    HOME=/config \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    RM_STATIC_DIR=/app/web

VOLUME ["/config", "/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/v1/system/health', timeout=4)" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["recompmanager", "serve"]
