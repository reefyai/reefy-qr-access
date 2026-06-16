#!/usr/bin/env python3
"""Full-pipeline benchmark: decode -> detect -> QR-decode, end to end.

Drives the REAL qr-access pipeline (`video_decode` + `pipeline`) against a
synthetic RTSP stream that shows a known QR token, and reports the metrics
the perf-regression gate asserts on:

  - fps           : end-to-end frames processed per second
  - cpu_cores     : total CPU (this process incl. in-proc detect/QR-decode
                    + the decode ffmpeg child) per wall-second
  - decode_ms / detect_ms / qrdecode_ms : per-frame stage latency
  - ok            : the known token decoded (correctness gate)

One backend per invocation (cpu | igpu | gpu) so the caller controls the
environment (e.g. CUDA_VISIBLE_DEVICES="" to force a true CPU run on a GPU
box). Emits a JSON line: RESULT_JSON {...}.

Reuses the synthetic-stream helpers from multicam_decode.py.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

_here = Path(__file__).resolve()
sys.path.insert(0, str(_here.parent))           # tests/e2e (multicam_decode)
if len(_here.parents) > 2:
    sys.path.insert(0, str(_here.parents[2]))   # repo root (video_decode, pipeline)

import video_decode  # noqa: E402
import pipeline  # noqa: E402
from multicam_decode import (  # noqa: E402
    gen_qr_video, ensure_mediamtx, wait_for_paths, proc_cpu, TOKEN, RES)


def _self_cpu():
    t = os.times()
    return t.user + t.system


def _decode_qr(crop):
    import cv2
    if crop.size == 0:
        return None
    v, _, _ = cv2.QRCodeDetector().detectAndDecode(crop)
    return v or None


def run(backend, url, seconds, model_size):
    decode_backend = pipeline.decode_backend_for(backend)
    reader = video_decode.open_reader(url, decode_backend, name=backend)
    if reader is None:
        return {'backend': backend, 'ok': False, 'error': 'open failed'}
    detector, actual = pipeline.build_detector(backend, model_size)

    # warmup (export/compile happens here for igpu/gpu)
    ok, frame = reader.read()
    if ok:
        detector.detect(frame, conf=0.3)

    child_pid = getattr(getattr(reader, '_proc', None), 'pid', None)
    dec = det = qr = 0.0
    frames = 0
    decoded = False
    c0 = _self_cpu()
    ch0 = proc_cpu(child_pid) if child_pid else 0.0
    w0 = time.time()
    deadline = w0 + seconds
    while time.time() < deadline:
        t0 = time.perf_counter()
        ok, frame = reader.read()
        t1 = time.perf_counter()
        if not ok:
            break
        dets = detector.detect(frame, conf=0.3)
        t2 = time.perf_counter()
        for (x1, y1, x2, y2, _c) in dets:
            if _decode_qr(frame[y1:y2, x1:x2]) == TOKEN:
                decoded = True
        t3 = time.perf_counter()
        dec += t1 - t0
        det += t2 - t1
        qr += t3 - t2
        frames += 1

    wall = max(time.time() - w0, 1e-6)
    cpu = (_self_cpu() - c0)
    if child_pid:
        cpu += max(0.0, proc_cpu(child_pid) - ch0)
    reader.release()

    n = max(frames, 1)
    return {
        'backend': backend,
        'pipeline': actual,
        'decode': video_decode.get_active() or decode_backend,
        'frames': frames,
        'fps': round(frames / wall, 1),
        'cpu_cores': round(cpu / wall, 2),
        'decode_ms': round(dec / n * 1000, 1),
        'detect_ms': round(det / n * 1000, 1),
        'qrdecode_ms': round(qr / n * 1000, 1),
        'decoded': decoded,
        'ok': decoded,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--backend', required=True, choices=['cpu', 'igpu', 'gpu'])
    ap.add_argument('--res', default='720p', choices=list(RES))
    ap.add_argument('--fps', type=int, default=15)
    ap.add_argument('--seconds', type=int, default=15)
    ap.add_argument('--model-size', default='n', choices=['n', 's', 'm', 'l'])
    ap.add_argument('--port', type=int, default=8554)
    ap.add_argument('--workdir', default='/tmp/pipeline_bench')
    args = ap.parse_args()

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    res = RES[args.res]
    qr = gen_qr_video(workdir, TOKEN, args.seconds + 8, args.fps, res)
    mtx = ensure_mediamtx(workdir)

    env = dict(os.environ, MTX_RTSPADDRESS=f':{args.port}')
    server = subprocess.Popen([str(mtx)], cwd=workdir, env=env,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
    time.sleep(2)
    url = f'rtsp://127.0.0.1:{args.port}/cam0'
    pub = subprocess.Popen(
        ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-re',
         '-stream_loop', '-1', '-i', str(qr), '-c', 'copy', '-f', 'rtsp', url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ready = wait_for_paths(1, args.port)
        print(f'[bench] backend={args.backend} res={args.res} '
              f'streams_ready={len(ready)} measuring {args.seconds}s...')
        result = run(args.backend, url, args.seconds, args.model_size)
    finally:
        pub.kill()
        server.kill()

    result['res'] = args.res
    result['fps_stream'] = args.fps
    print('RESULT_JSON ' + json.dumps(result))
    sys.exit(0 if result.get('ok') else 1)


if __name__ == '__main__':
    main()
