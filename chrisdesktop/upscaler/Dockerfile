# =============================================================================
#  AI Video Upscaler
#  Uses Real-ESRGAN to upscale videos < 1080p
#  Lowest priority: pauses when ripping/transcoding is active
# =============================================================================

FROM nvidia/cuda:12.0.0-base-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-dev \
    ffmpeg \
    libgl1-mesa-glx \
    libglib2.0-0 \
    wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Pin torchvision to 0.15.x — newer versions removed functional_tensor used by basicsr
RUN pip3 install --no-cache-dir \
    torch==2.0.1 torchvision==0.15.2 \
    --index-url https://download.pytorch.org/whl/cu118

# Install remaining dependencies
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Download Real-ESRGAN model weights
RUN mkdir -p /models && \
    wget -q -O /models/RealESRGAN_x2plus.pth \
        "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.1/RealESRGAN_x2plus.pth" && \
    wget -q -O /models/RealESRGAN_x4plus.pth \
        "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth" && \
    wget -q -O /models/RealESRGAN_x4plus_anime_6B.pth \
        "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.2.4/RealESRGAN_x4plus_anime_6B.pth"

COPY upscale.py .

# Graceful stop: Docker sends SIGTERM, our signal handler saves checkpoint
STOPSIGNAL SIGTERM

CMD ["python3", "-u", "upscale.py"]
