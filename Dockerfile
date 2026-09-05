# cudnn-runtime (not -devel): we don't compile CUDA, so -devel's toolkit/nvcc
# is dead weight. But onnxruntime's CUDA EP (gpu detector) needs a *system*
# CUDA 12 + cuDNN 9 - torch's pip CUDA-13 libs don't satisfy it - so we can't
# go all the way to -base. cudnn-runtime provides CUDA 12.9 + cuDNN 9 without
# the toolkit. GPU driver comes from the container runtime (CDI).
# See docs/gpu-pipeline.md.
FROM nvidia/cuda:12.9.0-cudnn-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive

ARG INTEL_OPENCL_VERSION=24.39.31294.20-1032~24.04
ARG INTEL_MEDIA_VERSION=24.3.4-1018~24.04
ARG INTEL_LEVEL_ZERO_VERSION=24.39.31294.20-1032~24.04
ARG LEVEL_ZERO_LOADER_VERSION=1.17.44.0-1022~24.04
ARG LIBVA_VERSION=2.22.0.2-87~u24.04

# System deps for OpenCV, pyzbar, and repository setup. ffmpeg/ffprobe
# (system build, VAAPI + NVDEC capable) drive the hardware video-decode path.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg \
    python3 python3-pip python3-dev \
    libzbar0 \
    libgl1 libglib2.0-0 \
    ffmpeg \
    fonts-dejavu-core \
    && install -d -m 0755 /usr/share/keyrings \
    && curl -fsSL https://repositories.intel.com/gpu/intel-graphics.key \
        | gpg --dearmor -o /usr/share/keyrings/intel-graphics.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/intel-graphics.gpg] https://repositories.intel.com/gpu/ubuntu noble client" \
        > /etc/apt/sources.list.d/intel-gpu.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        intel-opencl-icd=${INTEL_OPENCL_VERSION} \
        intel-media-va-driver-non-free=${INTEL_MEDIA_VERSION} \
        libze-intel-gpu1=${INTEL_LEVEL_ZERO_VERSION} \
        libze1=${LEVEL_ZERO_LOADER_VERSION} \
        ocl-icd-libopencl1 \
        libva2=${LIBVA_VERSION} \
        libva-drm2=${LIBVA_VERSION} \
        vainfo \
        clinfo \
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

COPY onvif_discovery.py qr_live.py qr_tracks.py video_decode.py pipeline.py run.py ./
COPY web/ web/
COPY reefy/ reefy/
# tests/ carries the full-pipeline perf benchmark (pipeline_bench.py) the
# perf-regression gate runs inside the image. Small; no test deps installed.
COPY tests/ tests/
COPY tools/ tools/

# Keep hardware selection and fallback regressions from reaching GHCR. These
# tests mock cameras and accelerators, so they are deterministic during build.
RUN python3 -m unittest \
    tests.test_video_decode \
    tests.test_pipeline \
    tests.test_perf_regression \
    tests.test_onvif_opener \
    tests.test_onvif_discovery

CMD ["python3", "run.py"]
