# Chromium renders the HUD and ffmpeg does the compositing, so the image needs
# both. Everything is pinned to the image rather than the host, which is the
# point of preferring Docker here: an identical render on any machine.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --no-cache-dir . \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    # Readable by any uid, so `docker run --user $(id -u)` still finds Chromium
    # and writes output files you own rather than root-owned ones.
    && chmod -R a+rX /opt/playwright

# Renders read and write here; mount your footage folder onto it.
WORKDIR /media

ENTRYPOINT ["dji-overlay"]
CMD ["--help"]
