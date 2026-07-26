# =============================================================================
#  AI Video Upscaler
#  Uses Real-ESRGAN to upscale videos < 1080p
#  Lowest priority: pauses when ripping/transcoding is active
# =============================================================================

FROM nvidia/cuda:12.3.1-cudnn9-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-dev \
    ffmpeg \
    libgl1-mesa-glx \
    libglib2.0-0 \
    wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install PyTorch with CUDA support first (large download)
RUN pip3 install --no-cache-dir \
    torch torchvision \
    --index-url https://download.pytorch.org/whl/cu121

# Install remaining dependencies
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Download Real-ESRGAN model weights
RUN mkdir -p /models && \
    wget -q -O /models/RealESRGAN_x4plus.pth \
        "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth" && \
    wget -q -O /models/RealESRGAN_x4plus_anime_6B.pth \
        "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.2.4/RealESRGAN_x4plus_anime_6B.pth"

COPY upscale.py .

# Graceful stop: Docker sends SIGTERM, our signal handler saves checkpoint
STOPSIGNAL SIGTERM

CMD ["python3", "-u", "upscale.py"]
