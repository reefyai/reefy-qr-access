# -base (not -devel/-runtime): we don't compile CUDA, and torch + onnxruntime
# bring their own cuDNN/cuBLAS/CUDA-runtime via pip (nvidia-* wheels), so the
# base image's system CUDA libs are redundant. The GPU driver comes from the
# container runtime (CDI). See docs/gpu-pipeline.md.
FROM nvidia/cuda:12.9.0-base-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive

# System deps for OpenCV, pyzbar.
# ffmpeg/ffprobe (system build, VAAPI + NVDEC capable) drive the
# hardware video-decode path; intel-media-va-driver + libva provide the
# Intel iGPU VAAPI backend. See docs/decode-backends.md.
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-dev \
    libzbar0 \
    libgl1 libglib2.0-0 \
    ffmpeg \
    intel-media-va-driver libva2 libva-drm2 vainfo \
    intel-opencl-icd \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Layers below are ordered most-stable -> most-volatile so a typical
# version bump only invalidates the small layers near the bottom.
# Devices then pull a few hundred kB instead of the full ~6.7 GB
# torch/tensorrt blob each time.

# Heavyweight: torch is the bulk of the image (qrdet/PyTorch cpu detector).
RUN pip3 install --break-system-packages --no-cache-dir \
    torch

# ML pipeline. Detector backends (see pipeline.py):
#   cpu  -> qrdet / PyTorch
#   igpu -> ultralytics OpenVINO (Intel iGPU)
#   gpu  -> ONNX via onnxruntime-gpu (CUDA EP)
# The gpu path runs an ONNX model through onnxruntime-gpu (~3 ms detect, on
# par with a TensorRT engine) instead of ultralytics' TensorRT engine
# export - whose dep chain (modelopt / onnx-graphsurgeon / ...) grows across
# versions and breaks the build unattended. ONNX export (onnx+onnxslim) and
# onnxruntime are stable, so these deps can float; the perf-regression gate
# (tools/run_perf_regression.py) guards against any drift.
RUN pip3 install --break-system-packages --no-cache-dir \
    ultralytics \
    openvino \
    onnx \
    onnxslim \
    onnxruntime-gpu \
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

COPY qr_live.py qr_tracks.py video_decode.py pipeline.py run.py ./
COPY web/ web/
COPY reefy/ reefy/
# tests/ carries the full-pipeline perf benchmark (pipeline_bench.py) the
# perf-regression gate runs inside the image. Small; no test deps installed.
COPY tests/ tests/
COPY tools/ tools/

CMD ["python3", "run.py"]
