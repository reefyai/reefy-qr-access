"""Hardware-accelerated video decode backend selection.

QR Access decodes every incoming RTSP frame before the detector looks
for QR codes. This module picks the cheapest decode path available on
the host and exposes one uniform reader that yields BGR numpy frames,
whatever the backend:

    nvdec   NVIDIA GPU (CUDA)        - ffmpeg -hwaccel cuda
    vaapi   Intel / AMD iGPU         - ffmpeg -hwaccel vaapi (/dev/dri)
    cpu     software (always works)  - cv2.VideoCapture (the proven path)

Design notes (see docs/decode-backends.md):

- The CPU path stays on cv2.VideoCapture exactly as before, so CPU-only
  devices (most of the fleet) see zero behaviour change.
- The hardware paths shell out to the SYSTEM ffmpeg binary and read raw
  bgr24 frames from its stdout. We use the system ffmpeg (not PyAV /
  not opencv's bundled ffmpeg) precisely because only the system build
  carries VAAPI / NVDEC; the pip wheels of both bundle a HW-less ffmpeg.
- Detection is attempt-as-truth: a backend is only reported as active
  once a real decode has produced a frame. Any failure (missing driver,
  unsupported codec/profile, busy device) falls back to CPU cleanly - a
  bad iGPU driver must never take a door offline.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading

RENDER_NODE = '/dev/dri/renderD128'


class StreamAuthenticationError(RuntimeError):
    """The camera explicitly rejected RTSP credentials or permission."""


# --- backend detection ------------------------------------------------

def _nvdec_available():
    """True when a usable NVIDIA GPU is present (driver loaded)."""
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def _vaapi_present():
    """Cheap pre-filter: a DRM render node exists. Not proof the driver
    decodes this codec - that is settled by the real open attempt."""
    return os.path.exists(RENDER_NODE)


def detect_backend(forced='auto'):
    """Resolve the decode backend. 'auto' probes NVDEC -> VAAPI -> CPU.
    A forced value (cpu/vaapi/nvdec) is returned as-is so an operator can
    pin or disable hardware decode via DECODE_BACKEND."""
    if forced and forced != 'auto':
        return forced
    if _nvdec_available():
        return 'nvdec'
    if _vaapi_present():
        return 'vaapi'
    return 'cpu'


def backend_label(backend):
    """Human label for the Settings -> Detector panel."""
    return {
        'nvdec': 'NVDEC (NVIDIA)',
        'vaapi': 'VAAPI (Intel/AMD iGPU)',
        'cpu': 'CPU (software)',
    }.get(backend, backend)


# --- stream probing ---------------------------------------------------

def _probe_failure(stderr):
    """Return a safe code and operator-facing reason for probe failure.

    Never include ffprobe's raw stderr here. It can contain the complete
    RTSP URL, including the camera username and password.
    """
    text = stderr.lower() if isinstance(stderr, str) else ''
    if '401 unauthorized' in text or 'server returned 401' in text:
        return ('auth',
                'camera rejected RTSP authentication (HTTP 401); verify the '
                'camera username and password saved for this door')
    if '403 forbidden' in text or 'server returned 403' in text:
        return ('forbidden',
                'camera rejected RTSP access (HTTP 403); verify this account '
                'has permission to view the selected stream')
    if '404 not found' in text or 'server returned 404' in text:
        return ('not_found',
                'camera rejected the RTSP stream path (HTTP 404)')
    if 'connection refused' in text:
        return ('refused',
                'RTSP connection refused; verify the camera address and port')
    if 'timed out' in text or 'connection timeout' in text:
        return ('timeout',
                'RTSP connection timed out; verify camera reachability')
    if 'no route to host' in text or 'network is unreachable' in text:
        return ('unreachable',
                'camera network is unreachable from this device')
    return 'other', 'unclassified ffprobe error'


def _probe_failure_reason(stderr):
    """Backward-compatible reason-only helper used by tests and logs."""
    return _probe_failure(stderr)[1]


def probe_stream_diagnostic(url, timeout=15, name='camera'):
    """Probe an RTSP stream and return width, height, fps, failure code.

    The failure code is None on success. Raw ffprobe output is never returned
    or logged because it can contain camera credentials.
    """
    cmd = [
        'ffprobe', '-v', 'error',
        '-rtsp_transport', 'tcp',
        '-select_streams', 'v:0',
        '-show_entries', 'stream=width,height,r_frame_rate',
        '-of', 'json', url,
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout,
                             text=True)
        if out.returncode != 0:
            code, reason = _probe_failure(getattr(out, 'stderr', ''))
            print(f"[WARN] [{name}] RTSP probe failed: {reason}")
            return 0, 0, 0.0, code
        info = json.loads(out.stdout)['streams'][0]
        w = int(info.get('width') or 0)
        h = int(info.get('height') or 0)
        rate = info.get('r_frame_rate') or '0/1'
        num, _, den = rate.partition('/')
        fps = (float(num) / float(den)) if den and float(den) else 15.0
        return w, h, (fps or 15.0), None
    except subprocess.TimeoutExpired:
        reason = 'RTSP connection timed out; verify camera reachability'
        print(f"[WARN] [{name}] RTSP probe failed: {reason}")
        return 0, 0, 0.0, 'timeout'
    except Exception:
        print(f"[WARN] [{name}] RTSP probe failed: unclassified ffprobe error")
        return 0, 0, 0.0, 'other'


def probe_stream(url, timeout=15, name='camera'):
    """Return (width, height, fps) for an RTSP stream via ffprobe, or
    (0, 0, 0.0) on failure. The ffmpeg-pipe reader needs the frame
    geometry up front to slice fixed-size rawvideo frames off stdout."""
    w, h, fps, _failure = probe_stream_diagnostic(
        url, timeout=timeout, name=name)
    return w, h, fps


# --- ffmpeg-pipe hardware decoder -------------------------------------

class FFmpegReader:
    """Decodes an RTSP stream with the system ffmpeg (VAAPI or NVDEC) and
    yields BGR numpy frames read off ffmpeg's stdout. Mirrors the slice
    of the cv2.VideoCapture API that CameraReader.run() uses:
    open()/read()/release() + width/height/fps."""

    def __init__(self, url, backend, name='camera'):
        self.url = url
        self.backend = backend
        self.name = name
        self.width = 0
        self.height = 0
        self.fps = 15.0
        self._proc = None
        self._frame_bytes = 0

    def open(self):
        self.width, self.height, self.fps, failure = probe_stream_diagnostic(
            self.url, name=self.name)
        if failure in ('auth', 'forbidden'):
            raise StreamAuthenticationError(failure)
        if not (self.width and self.height):
            print(f"[WARN] [{self.name}] ffprobe failed; "
                  f"cannot start {self.backend} decode")
            return False
        self._frame_bytes = self.width * self.height * 3

        if self.backend == 'vaapi':
            hw = ['-hwaccel', 'vaapi', '-hwaccel_device', RENDER_NODE]
        elif self.backend == 'nvdec':
            hw = ['-hwaccel', 'cuda']
        else:
            hw = []

        cmd = [
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
            '-rtsp_transport', 'tcp',
            '-fflags', 'nobuffer', '-flags', 'low_delay',
            *hw,
            '-i', self.url,
            '-an', '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-',
        ]
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0)
        except Exception as e:
            print(f"[WARN] [{self.name}] ffmpeg spawn failed: {e}")
            return False

        # Drain stderr so a full pipe buffer never blocks ffmpeg.
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        print(f"[INFO] [{self.name}] Stream opened ({self.backend}): "
              f"{self.width}x{self.height} @ {self.fps:.1f} FPS")
        return True

    def _drain_stderr(self):
        try:
            for line in iter(self._proc.stderr.readline, b''):
                msg = line.decode('utf-8', 'replace').strip()
                if msg:
                    print(f"[WARN] [{self.name}] ffmpeg: {msg}")
        except Exception:
            pass

    def _read_exact(self, n):
        """Read exactly n bytes off the pipe (one rawvideo frame), or
        return None at EOF / on short read (ffmpeg died -> reconnect)."""
        buf = bytearray()
        while len(buf) < n:
            chunk = self._proc.stdout.read(n - len(buf))
            if not chunk:
                return None
            buf.extend(chunk)
        return buf

    def read(self):
        """Return (ok, frame_bgr). ok=False signals the caller to
        reconnect, matching cv2.VideoCapture.read() semantics."""
        if self._proc is None:
            return False, None
        buf = self._read_exact(self._frame_bytes)
        if buf is None:
            return False, None
        import numpy as np
        frame = np.frombuffer(bytes(buf), np.uint8).reshape(
            self.height, self.width, 3)
        return True, frame

    def release(self):
        if self._proc is not None:
            try:
                self._proc.kill()
                self._proc.wait(timeout=2)
            except Exception:
                pass
            self._proc = None


# --- cv2 software reader (the proven CPU path, unchanged behaviour) ----

class Cv2Reader:
    """Software decode via cv2.VideoCapture - the original path. Wrapped
    in the same open()/read()/release() shape so CameraReader.run() is
    backend-agnostic."""

    def __init__(self, url, name='camera'):
        self.url = url
        self.name = name
        self.width = 0
        self.height = 0
        self.fps = 15.0
        self._cap = None

    def open(self):
        import cv2
        # Minimize FFmpeg internal buffer to reduce RTSP latency.
        os.environ['OPENCV_FFMPEG_CAPTURE_OPTIONS'] = \
            'rtsp_transport;tcp|fflags;nobuffer|flags;low_delay|max_delay;500000'
        cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            return False
        self._cap = cap
        self.width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
        print(f"[INFO] [{self.name}] Stream opened (cpu): "
              f"{self.width}x{self.height} @ {self.fps:.1f} FPS")
        return True

    def read(self):
        if self._cap is None:
            return False, None
        return self._cap.read()

    def release(self):
        if self._cap is not None:
            self._cap.release()
            self._cap = None


# --- unified opener + active-backend tracking -------------------------

# The backend a real stream actually decoded with, set once the first
# reader opens. Differs from the *detected* backend when a hardware path
# failed and fell back to CPU. qr_live surfaces this in DETECTOR_INFO.
_ACTIVE = None
_ACTIVE_LOCK = threading.Lock()


def _set_active(backend):
    global _ACTIVE
    with _ACTIVE_LOCK:
        _ACTIVE = backend


def get_active():
    with _ACTIVE_LOCK:
        return _ACTIVE


def open_reader(url, backend, name='camera'):
    """Open a reader for `backend`, falling back to CPU on any hardware
    failure. Returns an opened reader, or None if even CPU could not
    open (caller retries). Records the actually-used backend."""
    if backend in ('vaapi', 'nvdec'):
        reader = FFmpegReader(url, backend, name)
        if reader.open():
            _set_active(backend)
            return reader
        print(f"[WARN] [{name}] {backend} decode unavailable; "
              f"falling back to CPU software decode")
        backend = 'cpu (hw failed)'

    reader = Cv2Reader(url, name)
    if reader.open():
        _set_active('cpu' if backend == 'cpu' else backend)
        return reader
    # OpenCV hides FFmpeg's useful stderr. Probe once after a failed CPU
    # open so an explicit authentication rejection can stop automatic
    # retries instead of incrementing the camera's lockout counter.
    if backend == 'cpu':
        _w, _h, _fps, failure = probe_stream_diagnostic(
            url, timeout=10, name=name)
        if failure in ('auth', 'forbidden'):
            raise StreamAuthenticationError(failure)
    return None
