# Matchday Control Center. Run with docker compose; see "Run with Docker" in README.md.
FROM ubuntu:24.04

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      alsa-utils ca-certificates ffmpeg fontconfig fonts-dejavu-core iproute2 iputils-ping \
      network-manager pulseaudio-utils python3 python3-venv \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

COPY requirements.txt .
RUN python3 -m venv /opt/venv \
 && pip install --no-cache-dir -r requirements.txt \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

COPY *.py ./
COPY templates templates
COPY static static

# Bundle MediaMTX so a new laptop needs no download on the pitch.
RUN python -c "import media_engine as m; assert m.ensure_mediamtx(), 'MediaMTX install failed'"

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:5000/tally/cam1', timeout=3)"

CMD ["python", "app.py"]
