#!/usr/bin/env python3
"""Synthetic multi-camera decode e2e + load benchmark.

Proves two things about the video_decode backend (docs/decode-backends.md):

  1. Correctness - a known QR token is *shown* on N synthetic RTSP
     streams, and our reader decodes that exact token on ALL N of them.
  2. Load - measures the total decode CPU (sum over the N reader ffmpeg
     processes) so a hardware backend can be compared head-to-head with
     software, at a chosen resolution and camera count.

It is self-contained: generates the QR video, serves N RTSP copies via a
local mediamtx, opens N readers through `video_decode.open_reader`,
confirms the token, and reports CPU. No real cameras involved.

Run (inside a container with ffmpeg + /dev/dri for the hardware path):

    python3 multicam_decode.py --cams 4 --backend both --res 1080p --seconds 20

Exit code is non-zero if any stream failed to decode the token.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

# Make video_decode importable from both the repo layout (root is two
# levels up from tests/e2e/) and a flat copy (alongside this script).
_here = Path(__file__).resolve()
sys.path.insert(0, str(_here.parent))
if len(_here.parents) > 2:
    sys.path.insert(0, str(_here.parents[2]))
import video_decode  # noqa: E402

TOKEN = 'REEFY-MULTICAM-TEST-9F3A'
MEDIAMTX_VERSION = 'v1.9.3'
MEDIAMTX_URL = (
    f'https://github.com/bluenviron/mediamtx/releases/download/'
    f'{MEDIAMTX_VERSION}/mediamtx_{MEDIAMTX_VERSION}_linux_amd64.tar.gz')

RES = {'360p': (640, 360), '720p': (1280, 720), '1080p': (1920, 1080)}


def sh(cmd, **kw):
    return subprocess.run(cmd, **kw)


def gen_qr_video(workdir, token, seconds, fps, res):
    """Render the token as a QR centred on a `res` canvas, encoded HEVC
    (matches what real IP cameras emit). Returns the file path.

    The QR is built from the module matrix with numpy + cv2 (no Pillow)
    so the test stays light on dependencies."""
    import cv2
    import numpy as np
    import qrcode
    qr = qrcode.QRCode(border=4, box_size=1)
    qr.add_data(token)
    qr.make(fit=True)
    matrix = np.array(qr.get_matrix(), dtype=np.uint8)
    img = (1 - matrix) * 255  # True module -> 0 (black), False -> 255
    img = np.kron(img, np.ones((12, 12), dtype=np.uint8))  # upscale modules
    png = workdir / 'qr.png'
    cv2.imwrite(str(png), img)
    out = workdir / 'qr.hevc'
    w, h = res
    side = int(min(w, h) * 0.6)  # QR fills ~60% of the short side
    # No B-frames + a 1s keyframe interval so the publishers' -stream_loop
    # -c copy joins land on clean GOP boundaries (otherwise a decoder
    # joining mid-GOP throws "Could not find ref with POC" and corrupts
    # frames, which would break QR decoding).
    sh(['ffmpeg', '-y', '-loglevel', 'error', '-loop', '1', '-i', str(png),
        '-t', str(seconds), '-r', str(fps),
        '-vf', (f'scale={side}:{side}:flags=neighbor,'
                f'pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:white,format=yuv420p'),
        '-c:v', 'libx265', '-x265-params',
        f'bframes=0:keyint={fps}:min-keyint={fps}:scenecut=0:log-level=error',
        str(out)], check=True)
    return out


def wait_for_paths(cams, port, timeout=25):
    """Poll each RTSP path with ffprobe until it is publishable, so the
    readers never race ahead of the publishers (404 / DESCRIBE failed)."""
    ready = set()
    deadline = time.time() + timeout
    while time.time() < deadline and len(ready) < cams:
        for i in range(cams):
            if i in ready:
                continue
            w, _, _ = video_decode.probe_stream(
                f'rtsp://127.0.0.1:{port}/cam{i}', timeout=3)
            if w:
                ready.add(i)
        if len(ready) < cams:
            time.sleep(1)
    return ready


def ensure_mediamtx(workdir):
    """Download the mediamtx RTSP server binary if not cached."""
    binp = workdir / 'mediamtx'
    if binp.exists():
        return binp
    tgz = workdir / 'mediamtx.tar.gz'
    print(f'[setup] downloading mediamtx {MEDIAMTX_VERSION}...')
    urllib.request.urlretrieve(MEDIAMTX_URL, tgz)
    with tarfile.open(tgz) as t:
        t.extractall(workdir)
    binp.chmod(0o755)
    return binp


def proc_cpu(pid):
    """utime+stime (seconds) for a pid from /proc/<pid>/stat."""
    try:
        with open(f'/proc/{pid}/stat') as f:
            parts = f.read().split()
        clk = os.sysconf('SC_CLK_TCK')
        return (int(parts[13]) + int(parts[14])) / clk
    except Exception:
        return 0.0


def decode_qr(frame):
    import cv2
    val, _, _ = cv2.QRCodeDetector().detectAndDecode(frame)
    return val or None


def run_backend(backend, cams, base_port, seconds, fps):
    """Open `cams` ffmpeg readers on the RTSP streams with `backend`,
    decode the QR off each, and measure each reader's decode CPU via
    /proc/<pid>/stat. Using FFmpegReader for every backend (cpu included)
    keeps the CPU measurement apples-to-apples - the cpu case is software
    ffmpeg, the vaapi/nvdec cases add -hwaccel, all measured identically.
    """
    readers, decoded, cpu0 = [], {}, {}
    for i in range(cams):
        url = f'rtsp://127.0.0.1:{base_port}/cam{i}'
        r = video_decode.FFmpegReader(url, backend, name=f'cam{i}')
        if not r.open():
            print(f'[{backend}] cam{i}: FAILED to open')
            continue
        readers.append((i, r))
        cpu0[i] = proc_cpu(r._proc.pid)

    deadline = time.time() + seconds
    frame_no = 0
    while time.time() < deadline and len(decoded) < len(readers):
        for i, r in readers:
            ok, frame = r.read()
            if not ok:
                continue
            # Check QR every few frames per cam until it is found once.
            if i not in decoded and frame_no % 4 == 0:
                val = decode_qr(frame)
                if val:
                    decoded[i] = val
        frame_no += 1

    total_cpu, wall = 0.0, max(1e-6, seconds)
    for i, r in readers:
        total_cpu += max(0.0, proc_cpu(r._proc.pid) - cpu0.get(i, 0.0))
        r.release()

    ok_all = (len(decoded) == cams and
              all(v == TOKEN for v in decoded.values()))
    return {
        'backend': backend, 'cams': cams,
        'decoded': len(decoded), 'ok': ok_all,
        'cpu_s': round(total_cpu, 2),
        'cpu_pct_per_core': round(100 * total_cpu / wall, 1),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cams', type=int, default=4)
    ap.add_argument('--backend', default='both',
                    choices=['cpu', 'vaapi', 'nvdec', 'both'])
    ap.add_argument('--res', default='1080p', choices=list(RES))
    ap.add_argument('--fps', type=int, default=15)
    ap.add_argument('--seconds', type=int, default=20)
    ap.add_argument('--port', type=int, default=8554)
    ap.add_argument('--workdir', default='/tmp/multicam')
    args = ap.parse_args()

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    res = RES[args.res]

    # Clip a bit longer than a run so no -stream_loop seam lands inside
    # the measurement window.
    qr = gen_qr_video(workdir, TOKEN, args.seconds + 8, args.fps, res)
    mtx = ensure_mediamtx(workdir)

    env = dict(os.environ, MTX_RTSPADDRESS=f':{args.port}')
    server = subprocess.Popen([str(mtx)], cwd=workdir, env=env,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
    time.sleep(2)

    def start_publishers():
        pubs = []
        for i in range(args.cams):
            url = f'rtsp://127.0.0.1:{args.port}/cam{i}'
            log = open(workdir / f'pub{i}.log', 'wb')
            pubs.append(subprocess.Popen(
                ['ffmpeg', '-hide_banner', '-loglevel', 'warning', '-re',
                 '-stream_loop', '-1', '-i', str(qr), '-c', 'copy',
                 '-f', 'rtsp', url],
                stdout=subprocess.DEVNULL, stderr=log))
        return pubs

    backends = (['cpu', 'vaapi'] if args.backend == 'both'
                else [args.backend])
    results = []
    try:
        # Fresh publishers per backend run so each run sees live streams
        # (a single long-lived publisher set proved flaky across runs).
        for b in backends:
            pubs = start_publishers()
            ready = wait_for_paths(args.cams, args.port)
            print(f'[run] backend={b} cams={args.cams} res={args.res} '
                  f'fps={args.fps}: {len(ready)}/{args.cams} streams ready, '
                  f'measuring {args.seconds}s...')
            results.append(run_backend(b, args.cams, args.port,
                                       args.seconds, args.fps))
            for p in pubs:
                p.kill()
            time.sleep(1)
    finally:
        server.kill()

    print(f'\n=== Multi-camera decode: {args.cams}x {args.res} HEVC ===')
    print(f'{"backend":<10}{"decoded":<10}{"CPU(s)":<10}'
          f'{"%/core":<10}{"result"}')
    rc = 0
    for r in results:
        status = 'PASS' if r['ok'] else 'FAIL'
        if not r['ok']:
            rc = 1
        print(f'{r["backend"]:<10}'
              f'{str(r["decoded"]) + "/" + str(r["cams"]):<10}'
              f'{r["cpu_s"]:<10}{r["cpu_pct_per_core"]:<10}{status}')
    if len(results) == 2 and results[0]['cpu_s'] and results[1]['cpu_s']:
        sw, hw = results[0]['cpu_s'], results[1]['cpu_s']
        print(f'\ndecode CPU: {results[1]["backend"]} uses '
              f'{hw / sw:.2f}x the software CPU '
              f'({(1 - hw / sw) * 100:.0f}% less)')
    sys.exit(rc)


if __name__ == '__main__':
    main()
