#!/usr/bin/env python3
"""Export qrdet's YOLOv8 to the accelerated formats used by pipeline.py.

  openvino : portable IR for the Intel-iGPU detector (bakeable at build).
  engine   : TensorRT engine for the NVIDIA detector. GPU-arch specific -
             must be built on the target device, not baked into the image.

Artifacts land in $MODEL_CACHE (default /models), cached idempotently.
Usage:
    python3 tools/export_models.py --backend openvino --model-size n
    python3 tools/export_models.py --backend engine   --model-size s
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pipeline  # noqa: E402

FMT = {'openvino': 'openvino', 'engine': 'engine'}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--backend', required=True,
                    choices=['openvino', 'engine'])
    ap.add_argument('--model-size', default='n', choices=['n', 's', 'm', 'l'])
    args = ap.parse_args()
    out = pipeline.ensure_export(args.model_size, FMT[args.backend])
    print(f"exported {args.backend} qrdet-{args.model_size} -> {out}")


if __name__ == '__main__':
    main()
