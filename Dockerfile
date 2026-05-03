FROM nvidia/cuda:12.9.0-devel-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive

# System deps for OpenCV, pyzbar
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-dev \
    libzbar0 \
    libgl1 libglib2.0-0 \
    ffmpeg \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Layers below are ordered most-stable -> most-volatile so a typical
# version bump only invalidates the small layers near the bottom.
# Devices then pull a few hundred kB instead of the full ~6.7 GB
# torch/tensorrt blob each time.

# Heavyweight + stable: torch + tensorrt are the bulk of the image.
RUN pip3 install --break-system-packages --no-cache-dir \
    torch \
    tensorrt

# ML pipeline (mid-weight, changes occasionally).
RUN pip3 install --break-system-packages --no-cache-dir \
    ultralytics \
    opencv-python-headless \
    numpy

# QR detection / decoding (small, changes rarely).
RUN pip3 install --break-system-packages --no-cache-dir \
    pyzbar \
    qrdet \
    zxing-cpp \
    deqr

# Web + utility deps (small; most likely place for additions).
RUN pip3 install --break-system-packages --no-cache-dir \
    flask \
    requests \
    qrcode \
    zeroconf \
    pyyaml \
    psutil

# Persistent model cache
ENV MODEL_CACHE=/models
ENV PYTHONUNBUFFERED=1
RUN mkdir -p /models

COPY qr_live.py run.py ./
COPY web/ web/
COPY reefy/ reefy/

CMD ["python3", "run.py"]
