"""Inference pipeline backend selection for qr-access.

A "pipeline backend" bundles the video decode backend with the YOLO
detector backend so all heavy compute runs on the same device:

    cpu  : cv2 software decode + qrdet YOLOv8 on PyTorch-CPU
    igpu : VAAPI decode        + qrdet YOLOv8 on OpenVINO (Intel/AMD iGPU)
    gpu  : NVDEC decode        + qrdet YOLOv8 on TensorRT (NVIDIA GPU)

Detection runs on the accelerator; only the small detected crop is read
back to host for the CPU-only QR-decode libraries (pyzbar/zxing). See
docs/decode-backends.md - on the real qrdet model, OpenVINO-iGPU detect
measured ~5x faster than PyTorch-CPU with identical token accuracy.

Every hardware path degrades cleanly to cpu: a missing driver, a failed
export, or an unusable device must never take a door offline.
"""

import os
import shutil
from pathlib import Path

import video_decode

PIPELINE_BACKENDS = ('cpu', 'igpu', 'gpu')

# Decode backend (video_decode) paired with each pipeline backend.
_DECODE_FOR = {'cpu': 'cpu', 'igpu': 'vaapi', 'gpu': 'nvdec'}


def _has_cuda():
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def detect_pipeline_backend(forced='auto'):
    """Resolve the pipeline backend. 'auto' picks gpu on NVIDIA, igpu on an
    Intel/AMD iGPU (render node present), else cpu. A forced value is
    returned as-is so an operator can pin or disable acceleration."""
    if forced and forced != 'auto':
        return forced
    if _has_cuda():
        return 'gpu'
    if os.path.exists(video_decode.RENDER_NODE):
        return 'igpu'
    return 'cpu'


def decode_backend_for(pipeline_backend):
    """The video_decode backend that pairs with this pipeline backend."""
    return _DECODE_FOR.get(pipeline_backend, 'cpu')


# --- detectors --------------------------------------------------------

class QrdetDetector:
    """qrdet YOLOv8 on PyTorch (CPU; CUDA if torch selects it). The
    original, always-available path."""

    def __init__(self, model_size):
        from qrdet import QRDetector
        self._d = QRDetector(model_size=model_size)
        self.model_size = model_size

    def detect(self, frame, conf=0.3):
        raw = self._d.detect(image=frame)
        return [(*[int(c) for c in d['bbox_xyxy']], float(d['confidence']))
                for d in raw if d['confidence'] >= conf]

    def info(self):
        return {'detect': 'PyTorch', 'detect_device': 'CPU'}


class UltralyticsDetector:
    """qrdet's YOLOv8 exported to OpenVINO (iGPU) or TensorRT (GPU), run
    through ultralytics so letterbox + NMS + box extraction are handled."""

    def __init__(self, model_path, device, detect_label, device_label):
        from ultralytics import YOLO
        self._m = YOLO(str(model_path), task='segment')
        self._device = device
        self._detect_label = detect_label
        self._device_label = device_label
        # Fail fast here (caller catches -> cpu fallback) rather than at
        # the first frame: a tiny dummy inference proves the device works.
        import numpy as np
        self._m(np.zeros((64, 64, 3), dtype=np.uint8), device=device,
                verbose=False)

    def detect(self, frame, conf=0.3):
        results = self._m(frame, conf=conf, device=self._device,
                          verbose=False)
        dets = []
        for r in results:
            for box in r.boxes:
                x1, y1, x2, y2 = [int(c) for c in box.xyxy[0]]
                dets.append((x1, y1, x2, y2, float(box.conf[0])))
        return dets

    def info(self):
        return {'detect': self._detect_label,
                'detect_device': self._device_label}


# --- model export (idempotent, cached) --------------------------------

def _model_cache():
    cache = Path(os.environ.get('MODEL_CACHE', '/tmp/qr-models'))
    cache.mkdir(parents=True, exist_ok=True)
    return cache


def ensure_export(model_size, fmt):
    """Export qrdet-<size> to `fmt` ('openvino'|'engine') into the model
    cache, returning the artifact path. Idempotent - skips if present.
    OpenVINO IR is portable (bakeable at build); TensorRT engines are
    GPU-arch specific and must be built on the target device."""
    cache = _model_cache()
    name = {'openvino': f'qrdet-{model_size}_openvino_model',
            'onnx': f'qrdet-{model_size}.onnx',
            'engine': f'qrdet-{model_size}.engine'}[fmt]
    out = cache / name
    if out.exists():
        return out

    from qrdet import QRDetector
    pt = QRDetector(model_size=model_size).model  # ultralytics YOLO
    kw = {'imgsz': 640}
    if fmt == 'engine':
        kw['half'] = True
    exported = Path(pt.export(format=fmt, **kw))

    # ultralytics writes next to the source .pt; move it into the cache.
    if exported.resolve() != out.resolve():
        if out.exists():
            shutil.rmtree(out) if out.is_dir() else out.unlink()
        shutil.move(str(exported), str(out))
    return out


def build_detector(pipeline_backend, model_size):
    """Build the detector for a pipeline backend, falling back to
    PyTorch-CPU on any failure. Returns (detector, actual_backend)."""
    if pipeline_backend == 'igpu':
        try:
            ov = ensure_export(model_size, 'openvino')
            return (UltralyticsDetector(ov, 'intel:gpu', 'OpenVINO',
                                        'Intel iGPU'), 'igpu')
        except Exception as e:
            print(f"[WARN] igpu detector unavailable ({type(e).__name__}: "
                  f"{e}); falling back to PyTorch-CPU")
    elif pipeline_backend == 'gpu':
        try:
            # ONNX + onnxruntime-gpu (CUDA EP) - ~3 ms detect, on par with a
            # TensorRT engine but without ultralytics' growing engine-export
            # dep chain (modelopt/graphsurgeon/...). ONNX export is stable
            # (onnx+onnxslim), so this stays upgrade-friendly.
            onnx = ensure_export(model_size, 'onnx')
            return (UltralyticsDetector(onnx, 0, 'ONNX Runtime (CUDA)',
                                        'NVIDIA GPU'), 'gpu')
        except Exception as e:
            print(f"[WARN] gpu detector unavailable ({type(e).__name__}: "
                  f"{e}); falling back to PyTorch-CPU")

    return QrdetDetector(model_size), 'cpu'
