# Verticals v3 — Daily YouTube Shorts Pipeline als Job-Container.
# Kein Idle-Daemon: Host-Cron startet pro Lauf `compose run --rm`.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=0 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/home/vert

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg bc curl ca-certificates fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1000 -s /bin/bash vert

WORKDIR /srv/docker/moneymaker/verticals

# torch zuerst als CPU-Build (sonst zieht openai-whisper das ~2,5-GB-CUDA-Rad)
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

USER vert
# Whisper-Modellcache liegt unter $HOME/.cache -> per Mount persistent
CMD ["bash", "daily_run.sh"]
