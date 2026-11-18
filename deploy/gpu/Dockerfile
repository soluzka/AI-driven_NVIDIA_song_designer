# The whole application in one container, on a GPU host.
#
# Not Cloudflare: a Worker has no GPU, only a 128 MB isolate, and Workers AI's
# catalog has no music model. This image is what the GPU host runs, and
# cloudflared inside it is what makes the container reachable from Cloudflare at
# a stable hostname, so the browser only ever talks to Cloudflare.
#
#   docker compose -f deploy/gpu/compose.yaml up -d --build
#
# The CUDA libraries ship inside the +cu126 torch wheels, so the base image only
# needs the Debian runtime packages the audio stack loads at import time.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive \
    HF_HOME=/data/hf \
    TORCH_HOME=/data/torch

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        git ffmpeg libsndfile1 libgomp1 ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# Outbound-only tunnel client: no inbound ports are opened on the host, which is
# what lets this run behind NAT or on a rented GPU box with a firewall.
RUN curl -fsSL -o /usr/local/bin/cloudflared \
        https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
 && chmod +x /usr/local/bin/cloudflared \
 && cloudflared --version

WORKDIR /app
COPY . /app

# One environment for all three services, the same shape install_all.ps1 makes on
# Windows. torch comes from the CUDA index first so nothing swaps it for a CPU
# build afterwards.
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip wheel "setuptools<81" \
 && /opt/venv/bin/pip install torch==2.14.1+cu126 torchaudio==2.14.1+cu126 \
        --index-url https://download.pytorch.org/whl/cu126 \
 && /opt/venv/bin/pip install -r requirements.txt -r downloader_service/requirements.txt \
 && /opt/venv/bin/pip install -r song_service/requirements.txt \
        --extra-index-url https://download.pytorch.org/whl/cu126

ENV PATH=/opt/venv/bin:$PATH \
    SONG_PORT=8090 \
    SONG_HOST=0.0.0.0 \
    PORT=5001

EXPOSE 5001 8080 8090

HEALTHCHECK --interval=30s --timeout=10s --start-period=180s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8090/health > /dev/null || exit 1

ENTRYPOINT ["/app/deploy/gpu/entrypoint.sh"]
