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

# GPU deps — --no-cache-dir saves ~3GB
# Removed: onnx, onnxslim, onnxruntime-gpu (not used at runtime)
# Removed: libavcodec-dev/libavformat-dev/libswscale-dev (build headers not needed)
RUN pip3 install --break-system-packages --no-cache-dir \
    opencv-python-headless \
    pyzbar \
    qrdet \
    ultralytics \
    numpy \
    tensorrt \
    torch \
    zxing-cpp \
    deqr \
    psutil

# Lighter deps
RUN pip3 install --break-system-packages --no-cache-dir \
    zeroconf \
    pyyaml \
    requests \
    flask \
    qrcode

# Persistent model cache
ENV MODEL_CACHE=/models
ENV PYTHONUNBUFFERED=1
RUN mkdir -p /models

COPY qr_live.py run.py ./
COPY web/ web/
COPY reefy/ reefy/

CMD ["python3", "run.py"]
