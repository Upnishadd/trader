# ─── Kronos Trading Bot (CUDA / NVIDIA GPU) ───────────────────────────────────
# Uses NVIDIA CUDA base image for RTX 4060 Ti (8GB) GPU acceleration via WSL2.

FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

WORKDIR /app

# System dependencies + Python 3.11
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 \
    python3.11-dev \
    python3-pip \
    git \
    curl \
    build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.11 /usr/bin/python \
    && ln -sf /usr/bin/python3.11 /usr/bin/python3

# ─── Python dependencies ──────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    # Install PyTorch with CUDA 12.4 support first (for RTX 4060 Ti)
    pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cu124 && \
    pip install --no-cache-dir -r requirements.txt

# ─── App source ───────────────────────────────────────────────────────────────
COPY . .

# Create runtime directories
RUN mkdir -p /app/logs /app/data /app/config

# ─── Ports & entrypoint ───────────────────────────────────────────────────────
EXPOSE 8080

# Health check — verifies dashboard is up
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD curl -f http://localhost:8080/ || exit 1

CMD ["python", "main.py"]
