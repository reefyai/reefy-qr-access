#!/usr/bin/env python3
"""
Live RTSP QR code detector with multi-door strike control.
Supports multiple camera+door pairs via YAML config.
Each door has its own RTSP camera, Shelly relay, and token whitelist.
All doors share a single GPU for YOLO inference.

Uses:
- OpenCV RTSP capture
- YOLOv8 TensorRT for QR region detection on GPU
- pyzbar with multi-trial preprocessing for QR decoding
- Shelly Gen2+ RPC API for relay control
- mDNS for Shelly auto-discovery
"""

import sys
import os
import time
import signal
import argparse
import threading
import queue
from collections import deque
import cv2
import numpy as np
import json
import yaml
import requests
from requests.auth import HTTPDigestAuth
from pathlib import Path

import video_decode
import pipeline

from qr_tracks import QrTracks
from datetime import datetime

try:
    from zeroconf import ServiceBrowser, Zeroconf
    HAS_ZEROCONF = True
except ImportError:
    HAS_ZEROCONF = False

try:
    from ultralytics import YOLO
    HAS_ULTRALYTICS = True
except ImportError:
    HAS_ULTRALYTICS = False

try:
    from qrdet import QRDetector
    HAS_QRDET = True
except ImportError:
    HAS_QRDET = False

try:
    from pyzbar.pyzbar import decode as pyzbar_decode
    HAS_PYZBAR = True
except ImportError:
    HAS_PYZBAR = False

try:
    import zxingcpp
    HAS_ZXINGCPP = True
except ImportError:
    HAS_ZXINGCPP = False


def _detect_gpu():
    """True only when a usable NVIDIA GPU is present (driver loaded).
    torch.cuda.is_available() checks the actual driver, so a CUDA image
    on a GPU-less host returns False. One source of truth so we never
    attempt GPU-only paths (TensorRT, h264_nvenc) on a CPU-only device."""
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        return False


# Single GPU switch, evaluated once at import. Drives detector backend,
# default model size, and the event-clip video encoder.
HAS_GPU = _detect_gpu()


def _gpu_name():
    """Marketing name of the active GPU (e.g. 'NVIDIA RTX 3060'), or ''."""
    if not HAS_GPU:
        return ''
    try:
        import torch
        return torch.cuda.get_device_name(0)
    except Exception:
        return 'GPU'


def _cpu_name():
    """First CPU model-name line from /proc/cpuinfo, or ''."""
    try:
        with open('/proc/cpuinfo') as f:
            for line in f:
                if line.startswith('model name'):
                    return line.split(':', 1)[1].strip()
    except Exception:
        pass
    return ''


# Runtime detector facts surfaced to the web UI (Settings -> System ->
# Detector). Static fields filled in main(); 'fps' and 'video_decode'
# refreshed each status tick by run_multi_door. Read in-process by
# web/app.py (/api/system).
DETECTOR_INFO = {}

# Video decode backend (nvdec/vaapi/cpu) used by every CameraReader.
# Resolved once in main() from --decode-backend / DECODE_BACKEND.
VIDEO_DECODE_BACKEND = 'cpu'


class CameraAuthenticationError(RuntimeError):
    """ONVIF rejected saved camera credentials."""

DEFAULT_CONFIG = {
    'doors': [{
        'name': 'Default Door',
        'camera': 'rtsp://<user>:<pass>@<camera-host>:554/stream2',
        'shelly': 'auto',
        'open_seconds': 5,
        'tokens': ['6f9c2a8d3b1e4f7a9c0d2e5f8a1b3c4d'],
    }]
}

_running = True
_reload_event = None
_camera_readers = []  # track active CameraReader threads for cleanup


def set_reload_event(event):
    """Set the reload event (called by run.py)."""
    global _reload_event
    _reload_event = event


def signal_handler(sig, frame):
    global _running
    print("\n[INFO] Shutting down...")
    _running = False


# --- mDNS discovery ---

# Module-level shared cache of {device_id: ip}, populated by background
# rediscovery from DoorController workers. Promoting to module scope so
# multiple doors share results from a single mDNS sweep, and so an HTTP
# failure on one door can invalidate just its own entry without losing
# the others.
_shelly_cache: dict[str, str] = {}
_shelly_cache_lock = threading.Lock()


def discover_shelly(timeout=5):
    """Auto-discover Shelly devices on the local network via mDNS.
    Returns dict: shelly_id -> ip  (e.g. 'shelly1minig3-aabbccddeeff' -> '10.0.0.5').
    Side effect: merges results into the module-level _shelly_cache so
    DoorController workers can read fresh data without re-running mDNS
    themselves.
    """
    if not HAS_ZEROCONF:
        print("[WARN] zeroconf not installed, cannot auto-discover Shelly")
        return {}

    discovered = {}

    class ShellyListener:
        def add_service(self, zc, stype, name):
            info = zc.get_service_info(stype, name)
            if info and info.parsed_addresses():
                ip = info.parsed_addresses()[0]
                # name is like "shelly1minig3-dcb4d9ca16cc._shelly._tcp.local."
                device_id = name.split('._shelly._tcp')[0]
                discovered[device_id] = ip

        def remove_service(self, zc, stype, name):
            pass

        def update_service(self, zc, stype, name):
            pass

    zc = Zeroconf()
    listener = ShellyListener()
    browser = ServiceBrowser(zc, "_shelly._tcp.local.", listener)

    print(f"[INFO] Discovering Shelly devices ({timeout}s)...")
    time.sleep(timeout)
    zc.close()

    for device_id, ip in discovered.items():
        print(f"[INFO] Found: {device_id} -> {ip}")

    # Merge into module-level cache so other doors (and subsequent
    # resolves on this door) see the result.
    with _shelly_cache_lock:
        _shelly_cache.update(discovered)

    return discovered


def _is_ipv4(s: str) -> bool:
    parts = s.split('.')
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


def resolve_shelly_address(shelly_spec, discovered_devices):
    """Resolve a Shelly specifier to an IP address.
    shelly_spec can be:
      - IP address: '10.0.0.5'
      - mDNS device ID: 'shelly1minig3-dcb4d9ca16cc'
      - 'auto': use first discovered device
    """
    if not shelly_spec or shelly_spec == 'auto':
        if discovered_devices:
            first_id = next(iter(discovered_devices))
            ip = discovered_devices[first_id]
            print(f"[INFO] Auto-selected Shelly: {first_id} -> {ip}")
            return ip
        print("[WARN] No Shelly devices discovered for 'auto'")
        return None

    # Check if it's an IP address
    parts = shelly_spec.split('.')
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        return shelly_spec

    # Try to match as mDNS device ID
    if shelly_spec in discovered_devices:
        ip = discovered_devices[shelly_spec]
        print(f"[INFO] Resolved {shelly_spec} -> {ip}")
        return ip

    # Partial match
    for device_id, ip in discovered_devices.items():
        if shelly_spec in device_id:
            print(f"[INFO] Resolved {shelly_spec} -> {device_id} -> {ip}")
            return ip

    print(f"[WARN] Could not resolve Shelly '{shelly_spec}'")
    return None


# --- Door controller ---

class DoorController:
    """Controls door strike via Shelly 1 Mini Gen3 relay.

    Takes a `device_spec` which can be either a literal IPv4 address or
    a Shelly mDNS device-id (e.g. 'shelly1g4-98a31677f6b8'). For the
    device-id form, a background worker keeps running mDNS until the
    device is found, then idles. On HTTP failure (Shelly rebooted, IP
    changed via DHCP, container network blip), the worker is re-armed
    and the cached IP invalidated so the next scan resolves fresh.

    No periodic polling. Discovery only fires when actually needed:
    initial resolve, after a failed call, or on cache miss.
    """

    def __init__(self, name, device_spec, open_seconds=5, password=None):
        self.name = name
        self.device_spec = device_spec
        self.open_seconds = open_seconds
        self._auth = HTTPDigestAuth("admin", password) if password else None
        self._last_open = 0
        self._cooldown = open_seconds  # re-fire allowed once the pulse ends (= beep length)
        self._lock = threading.Lock()
        self._session = requests.Session()
        if self._auth:
            self._session.auth = self._auth

        # Discovery worker plumbing. The event is "set" when the worker
        # should (re)attempt to find this device's IP via mDNS, and
        # "clear" while idle. Initial set kicks off first discovery.
        self._rediscover_event = threading.Event()
        self._stop = False
        # A scan that arrives while the relay is unresolved/unreachable
        # is queued (token, ts) instead of dropped; the discovery
        # worker fires it once the Shelly resolves, within
        # _PENDING_OPEN_TTL_S. Stops the "scan ignored" dead window
        # where the visitor did everything right and got nothing.
        self._pending_open = None
        if _is_ipv4(device_spec):
            # Literal IP - skip discovery, seed cache directly so
            # _resolve_ip() returns immediately.
            with _shelly_cache_lock:
                _shelly_cache[device_spec] = device_spec
        else:
            self._rediscover_event.set()
        threading.Thread(target=self._discovery_worker, daemon=True,
                         name=f'shelly-discover-{name}').start()

    def _resolve_ip(self):
        """Look up current IP for our device. None until first discovery
        succeeds or the cached IP is invalidated by a failed call."""
        if _is_ipv4(self.device_spec):
            return self.device_spec
        with _shelly_cache_lock:
            return _shelly_cache.get(self.device_spec)

    _PENDING_OPEN_TTL_S = 15

    def _queue_pending_open(self, token):
        with self._lock:
            self._pending_open = (token, time.time())

    def _take_pending_open(self):
        """Pop the queued open if still fresh, else None."""
        with self._lock:
            pending = self._pending_open
            self._pending_open = None
        if pending and time.time() - pending[1] <= self._PENDING_OPEN_TTL_S:
            return pending[0]
        return None

    def _discovery_worker(self):
        """Cycle mDNS until our device_id appears in the cache, then
        idle until the next failure re-arms the event. Single-burst per
        invocation: 5s discovery, then 10s sleep before next attempt
        (mDNS broadcasts are cheap but not free)."""
        while not self._stop:
            self._rediscover_event.wait()
            self._rediscover_event.clear()
            while not self._stop and not self._resolve_ip():
                print(f"[INFO] [{self.name}] Looking up Shelly "
                      f"'{self.device_spec}' via mDNS...")
                discover_shelly(timeout=5)
                if self._resolve_ip():
                    print(f"[INFO] [{self.name}] Shelly resolved -> "
                          f"{self._resolve_ip()}")
                    break
                print(f"[WARN] [{self.name}] mDNS miss for "
                      f"'{self.device_spec}', retrying in 10s")
                time.sleep(10)
            # Fire a scan that arrived while the relay was unresolved.
            token = self._take_pending_open()
            if token and not self._stop:
                print(f"[INFO] [{self.name}] firing queued open "
                      f"(scan arrived during rediscovery)")
                self.open(token)

    def _get(self, url):
        # Tight timeout so an unreachable Shelly fails fast and we can
        # invalidate + re-discover within a single scan attempt.
        return self._session.get(url, timeout=2)

    def open(self, token):
        """Open door strike. Returns True if command was sent."""
        with self._lock:
            now = time.time()
            if now - self._last_open < self._cooldown:
                return False
            ip = self._resolve_ip()
            # If the worker is still on its first pass, give it ~3s to
            # land before failing - the operator's first scan right
            # after container start should usually succeed.
            for _ in range(30):
                if ip:
                    break
                time.sleep(0.1)
                ip = self._resolve_ip()
            if not ip:
                ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                print(f"[{ts}] [{self.name}] DOOR ERROR: Shelly "
                      f"'{self.device_spec}' not yet discovered, "
                      f"open queued for resolve")
                self._queue_pending_open(token)
                self._rediscover_event.set()
                return False
            self._last_open = now

        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        url = (f"http://{ip}/rpc/Switch.Set"
               f"?id=0&on=true&toggle_after={self.open_seconds}")
        # Retry the cached IP briefly before declaring it stale: the
        # observed failure mode is a WiFi blip where the Shelly comes
        # back on the SAME address within seconds - a full mDNS
        # rediscovery (5s scan + 10s miss-retry) left the door dead
        # for ~20s for nothing.
        last_exc = None
        retries = (0, 0.5, 1.0)
        for attempt, delay in enumerate(retries, start=1):
            if delay:
                time.sleep(delay)
            try:
                resp = self._get(url)
                resp.raise_for_status()
                print(f"[{ts}] [{self.name}] DOOR OPEN for "
                      f"{self.open_seconds}s "
                      f"(token={token[:8]}...) shelly={resp.text}")
                return True
            except (requests.ConnectionError, requests.Timeout) as e:
                last_exc = e
                # Surface each miss so a slow relay_ms badge is
                # self-explanatory from the log, not archaeology.
                print(f"[WARN] [{self.name}] {ip} "
                      f"{type(e).__name__} on cached IP "
                      f"(attempt {attempt}/{len(retries)})")
                continue
            except Exception as e:
                print(f"[{ts}] [{self.name}] DOOR ERROR: {e}")
                self._reset_cooldown()
                return False

        # Still unreachable after retries: invalidate the cached IP,
        # queue this open, and arm the worker - it fires the open as
        # soon as the Shelly re-resolves (often the same IP).
        print(f"[{ts}] [{self.name}] DOOR ERROR: {ip} unreachable "
              f"({type(last_exc).__name__} x3), re-detecting Shelly; "
              f"open queued")
        with _shelly_cache_lock:
            _shelly_cache.pop(self.device_spec, None)
        self._reset_cooldown()
        self._queue_pending_open(token)
        self._rediscover_event.set()
        return False

    def _reset_cooldown(self):
        """A failed open must not burn the door cooldown - otherwise
        the queued/retried open (or the visitor's next scan) gets
        rejected as 'too soon' for a door that never actually opened."""
        with self._lock:
            self._last_open = 0

    def status(self):
        ip = self._resolve_ip()
        if not ip:
            return "error: not yet discovered"
        url = f"http://{ip}/rpc/Switch.GetStatus?id=0"
        try:
            resp = self._get(url)
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            return f"error: {e}"


class OnvifRelayController:
    """Door opener that drives an ONVIF camera's alarm relay output - no
    Shelly. Mirrors DoorController's interface (open(token) -> bool,
    cooldown, status()). On startup it puts the relay in Monostable mode
    with DelayTime = open_seconds (fail-secure: the camera's own timer
    re-locks the strike if this process dies); each open fires one
    SetRelayOutputState active. Reuses the same WS-Security auth as RTSP.
    """

    def __init__(self, name, ip, username='', password='', open_seconds=5,
                 relay_token='auto', device_url=''):
        self.name = name
        self.ip = ip
        self.username = username
        self.password = password
        self.open_seconds = open_seconds
        self.relay_token = None if (not relay_token or relay_token == 'auto') \
            else relay_token
        self.dev_url = (device_url.split()[0] if device_url
                        else f"http://{ip}/onvif/device_service")
        self._last_open = 0
        self._cooldown = open_seconds  # re-fire allowed once the pulse ends (= beep length)
        self._lock = threading.Lock()
        self._session = requests.Session()
        self._configured = False
        try:
            self._ensure_configured()
        except Exception as e:
            # Camera may be booting / unreachable at startup; open() retries.
            print(f"[WARN] [{name}] ONVIF relay init deferred: {e}")

    def _call(self, body, timeout=8):
        auth = (_onvif_ws_security_header(self.username, self.password)
                if self.username else '')
        env = ('<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"'
               ' xmlns:tds="http://www.onvif.org/ver10/device/wsdl"'
               ' xmlns:tt="http://www.onvif.org/ver10/schema">'
               f'{auth}<soap:Body>{body}</soap:Body></soap:Envelope>')
        return self._session.post(
            self.dev_url, data=env.encode(),
            headers={'Content-Type': 'application/soap+xml'}, timeout=timeout)

    def _ensure_configured(self):
        """Discover the relay token (when 'auto') and set Monostable +
        DelayTime=open_seconds. Raises on an unreachable camera."""
        import re  # module imports re locally per-function (see ONVIF fns)
        if not self.relay_token:
            r = self._call('<tds:GetRelayOutputs/>')
            m = re.search(r'token="([^"]+)"', r.text)
            if not m:
                raise RuntimeError('no ONVIF relay output found')
            self.relay_token = m.group(1)
        delay = f"PT{int(self.open_seconds)}S"
        self._call(
            f"<tds:SetRelayOutputSettings><tds:RelayOutputToken>{self.relay_token}"
            f"</tds:RelayOutputToken><tds:Properties><tt:Mode>Monostable</tt:Mode>"
            f"<tt:DelayTime>{delay}</tt:DelayTime><tt:IdleState>closed</tt:IdleState>"
            f"</tds:Properties></tds:SetRelayOutputSettings>")
        self._configured = True
        print(f"[INFO] [{self.name}] ONVIF relay {self.relay_token} "
              f"configured Monostable/{delay} at {self.ip}")

    def open(self, token):
        """Fire one Monostable relay pulse. Returns True if accepted."""
        with self._lock:
            now = time.time()
            if now - self._last_open < self._cooldown:
                return False
            self._last_open = now
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        try:
            if not self._configured or not self.relay_token:
                self._ensure_configured()
            r = self._call(
                f"<tds:SetRelayOutputState><tds:RelayOutputToken>{self.relay_token}"
                f"</tds:RelayOutputToken><tds:LogicalState>active</tds:LogicalState>"
                f"</tds:SetRelayOutputState>")
            if r.status_code == 200 and 'Fault' not in r.text:
                print(f"[{ts}] [{self.name}] DOOR OPEN (ONVIF relay "
                      f"{self.relay_token}) for {self.open_seconds}s "
                      f"(token={token[:8]}...)")
                return True
            print(f"[{ts}] [{self.name}] DOOR ERROR: ONVIF relay HTTP "
                  f"{r.status_code} {r.text[:160]}")
        except Exception as e:
            print(f"[{ts}] [{self.name}] DOOR ERROR: ONVIF relay "
                  f"unreachable: {type(e).__name__}: {e}")
        # A failed open must not burn the cooldown; re-resolve next time.
        with self._lock:
            self._last_open = 0
        self._configured = False
        return False

    def status(self):
        try:
            r = self._call('<tds:GetRelayOutputs/>', timeout=5)
            return ('ok' if (r.status_code == 200 and 'RelayOutputs' in r.text)
                    else f'error: HTTP {r.status_code}')
        except Exception as e:
            return f"error: {e}"


def onvif_relay_check(ip, username='', password='', timeout=5,
                      device_url=''):
    """Reachability probe for an ONVIF camera relay (used by the door
    health monitor). Returns {'ok': bool, 'error': str|None}."""
    try:
        auth = _onvif_ws_security_header(username, password) if username else ''
        env = ('<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"'
               ' xmlns:tds="http://www.onvif.org/ver10/device/wsdl">'
               f'{auth}<soap:Body><tds:GetRelayOutputs/></soap:Body></soap:Envelope>')
        url = (device_url.split()[0] if device_url
               else f"http://{ip}/onvif/device_service")
        r = requests.post(url, data=env.encode(),
                          headers={'Content-Type': 'application/soap+xml'},
                          timeout=timeout)
        if r.status_code == 200 and 'RelayOutputs' in r.text:
            return {'ok': True, 'error': None}
        return {'ok': False, 'error': f'HTTP {r.status_code}'}
    except Exception as e:
        return {'ok': False, 'error': f'{type(e).__name__}: {e}'[:200]}


# --- YOLO + QR decode ---

def get_tensorrt_engine_path(model_size='s'):
    cache_dir = Path(os.environ.get('MODEL_CACHE', '/tmp/qr-models'))
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"qrdet_{model_size}.engine"


def create_detector(model_size='s'):
    # TensorRT is GPU-only; don't even look for an engine on a CPU-only
    # host (avoids a pointless CUDA spin-up before the PyTorch fallback).
    if HAS_ULTRALYTICS and HAS_GPU:
        engine_path = get_tensorrt_engine_path(model_size)
        if engine_path.exists():
            print(f"[INFO] Loading TensorRT engine: {engine_path}")
            model = YOLO(str(engine_path), task='segment')
            return 'tensorrt', model

    if HAS_QRDET:
        detector = QRDetector(model_size=model_size)
        print(f"[INFO] Using YOLOv8-{model_size} (PyTorch)")
        return 'qrdet', detector

    print("[WARN] No YOLO detector available")
    return 'none', None


def create_decoder():
    if HAS_PYZBAR:
        print("[INFO] QR decoder: pyzbar")
        def decode(img):
            results = pyzbar_decode(img)
            return [r.data.decode('utf-8') for r in results]
        return decode

    if HAS_ZXINGCPP:
        print("[INFO] QR decoder: zxing-cpp (fallback)")
        def decode(img):
            results = zxingcpp.read_barcodes(img)
            return [r.text for r in results]
        return decode

    print("[ERROR] No QR decoder available")
    sys.exit(1)


def preprocess_variants(crop):
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    h, w = crop.shape[:2]
    variants = [crop]

    blk = max(51, (min(h, w) // 4) | 1)
    adapt = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                  cv2.THRESH_BINARY, blk, 10)
    variants.append(adapt)

    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(4, 4))
    clahe_img = clahe.apply(gray)
    _, clahe_otsu = cv2.threshold(clahe_img, 0, 255,
                                  cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    variants.append(clahe_otsu)

    up = cv2.resize(crop, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC)
    variants.append(up)
    variants.append(clahe_img)

    if h > 100 and w > 100:
        down = cv2.resize(crop, (w // 2, h // 2), interpolation=cv2.INTER_AREA)
        variants.append(down)

    blurred = cv2.GaussianBlur(gray, (0, 0), sigmaX=3)
    sharp = cv2.addWeighted(gray, 2.0, blurred, -1.0, 0)
    variants.append(sharp)

    _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    variants.append(otsu)

    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    glare_mask = cv2.inRange(hsv, (0, 0, 200), (180, 30, 255))
    if cv2.countNonZero(glare_mask) > 0:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        glare_mask = cv2.dilate(glare_mask, kernel, iterations=2)
        inpainted = cv2.inpaint(crop, glare_mask, 5, cv2.INPAINT_TELEA)
        variants.append(inpainted)

    return variants


def decode_with_preprocess(decode_fn, crop):
    for vimg in preprocess_variants(crop):
        try:
            tokens = decode_fn(vimg)
            if tokens:
                return tokens
        except Exception:
            continue


# Frame-stacking ring depth + cascade windows. On per-frame decode miss
# we median-stack a sliding subset of the last STACK_N crops (aligned
# to the current crop shape) and retry the decode pipeline. The cascade
# tries progressively wider windows so we catch both fast-shifting
# bands (small N suffices) and slow/static bands (need a wider window
# to see a phase shift). Cascade ordering matters: the cheapest stack
# is tried first; the cascade exits on the first decode hit.
#
# Why per-frame "band-attacking" preprocessing variants (vertical morph
# close, per-row dark-strip interpolation, FFT notch) were tried and
# dropped: on real banded videos they decoded the exact same frame set
# as the no-extra-variant pipeline. Whatever residual the stacked image
# is leaving behind on miss isn't band geometry - it's QR-module-level
# damage that no per-frame trick recovers.
#
# Benchmarked on two banded videos:
#   - "fast-shift" video: 80 -> 99 decoded frames (38% -> 47%)
#   - "slow-shift" video: 35 -> 101 decoded frames (17% -> 48%)
# On clean video the cascade never engages (single-frame succeeds first).
STACK_N = int(os.environ.get('REEFY_QR_STACK_N', '20'))
# Cascade windows for the stacked-retry path. First-hit wins.
STACK_CASCADE = [5, 10, 20]


def _resize_to(img, shape):
    h, w = shape[:2]
    if img.shape[:2] == (h, w):
        return img
    return cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)


def decode_with_stacking(decode_fn, crop, crop_ring):
    """Single-frame decode first; on miss, run a cascade of median-
    stacked retries with progressively wider windows. Returns
    (tokens, used_stack) - `used_stack` is True if any stacked retry
    is what produced the tokens (telemetry / debug).

    `crop_ring` is a per-camera deque(maxlen=STACK_N); the caller is
    responsible for holding one ring per ROI source. We mutate it
    in-place by appending the current crop after the decode attempt.
    """
    tokens = decode_with_preprocess(decode_fn, crop)
    used_stack = False
    if not tokens and len(crop_ring) >= 1:
        # YOLO ROI wobbles a few px between frames; align all prior
        # crops to the current shape before stacking. cv2.resize is
        # ~free compared to the decode work that follows.
        aligned = [_resize_to(c, crop.shape) for c in crop_ring] + [crop]
        for window in STACK_CASCADE:
            if len(aligned) < window:
                continue
            sub = aligned[-window:]
            stacked = (
                np.median(np.stack(sub, axis=0), axis=0).astype(np.uint8))
            tokens = decode_with_preprocess(decode_fn, stacked)
            if tokens:
                used_stack = True
                break
    crop_ring.append(crop)
    # decode_with_preprocess falls off the end (returns None) when every
    # variant fails. Normalise to an empty list so the caller's
    # `for token in tokens:` never sees None - latent bug in
    # decode_with_preprocess too, only exposed on streams with corrupt
    # HEVC frames where every preprocess variant misses.
    return tokens or [], used_stack


# Gap (seconds) of no decode activity after which a new frame is treated
# as a NEW visitor, so the stacking ring is reset before its decode. The
# ring only advances on detection frames, so without this it stays frozen
# with the previous visitor's crops indefinitely.
VISITOR_GAP_S = float(os.environ.get('REEFY_QR_VISITOR_GAP_S', '5'))


class StackingDecoder:
    """Per-camera median-stacking QR decoder.

    Owns the crop_ring used to recover hard-to-read QRs by median-stacking
    recent frames, AND the policy for resetting it so one visitor's frames
    never stack into another visitor's decode. The ring only advances on
    detection frames, so without a reset it can stay frozen with a previous
    visitor's QR between visitors; the next person's single-frame miss could
    then median-stack those stale crops and decode the previous token.
    Resetting between visitors / after a decode keeps each decode scoped to
    the current presentation.
    """

    def __init__(self, decode_fn, *, gap_s=VISITOR_GAP_S, maxlen=STACK_N):
        self._decode_fn = decode_fn
        self._ring = deque(maxlen=maxlen)
        self._gap_s = gap_s
        self._last_ts = None

    def decode(self, crop, now):
        """Decode one ROI crop. `now` is a wallclock used to detect the
        inter-visitor gap. Returns (tokens, used_stack)."""
        # New visitor after a quiet gap: drop the previous visitor's stale
        # crops so this decode can't median-stack their QR and return the
        # previous token. The ring only advances on detection frames, so
        # without this it stays frozen between visitors indefinitely.
        if self._last_ts is not None and now - self._last_ts > self._gap_s:
            self._ring.clear()
        self._last_ts = now
        tokens, used_stack = decode_with_stacking(
            self._decode_fn, crop, self._ring)
        # A successful decode consumes the presentation; reset so the next
        # frame starts fresh (also closes the within-gap tailgating window).
        if tokens:
            self._ring.clear()
        return tokens, used_stack


def detect_qr_regions(det_type, det_model, frame, conf=0.3):
    if det_type == 'tensorrt':
        results = det_model(frame, conf=conf, verbose=False)
        dets = []
        for result in results:
            for box in result.boxes:
                x1, y1, x2, y2 = [int(c) for c in box.xyxy[0]]
                dets.append((x1, y1, x2, y2, float(box.conf[0])))
        return dets

    if det_type == 'qrdet':
        raw = det_model.detect(image=frame)
        return [(*[int(c) for c in d['bbox_xyxy']], d['confidence'])
                for d in raw]

    return []


def discover_onvif_cameras(timeout=3):
    """Discover ONVIF cameras on the network via WS-Discovery.
    Returns list of dicts with ip, uuid, name, hardware, xaddr.
    """
    import socket
    import re
    from urllib.parse import unquote

    PROBE = ('<?xml version="1.0" encoding="utf-8"?>'
             '<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"'
             ' xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"'
             ' xmlns:wsd="http://schemas.xmlsoap.org/ws/2005/04/discovery"'
             ' xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
             '<soap:Header>'
             '<wsa:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</wsa:Action>'
             '<wsa:MessageID>urn:uuid:12345678-1234-1234-1234-123456789012</wsa:MessageID>'
             '<wsa:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</wsa:To>'
             '</soap:Header>'
             '<soap:Body><wsd:Probe>'
             '<wsd:Types>dn:NetworkVideoTransmitter</wsd:Types>'
             '</wsd:Probe></soap:Body></soap:Envelope>')

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(timeout)
    sock.sendto(PROBE.encode(), ('239.255.255.250', 3702))

    print(f"[INFO] Discovering ONVIF cameras ({timeout}s)...")
    cameras = []
    found_ips = set()
    try:
        while True:
            data, addr = sock.recvfrom(65535)
            ip = addr[0]
            if ip in found_ips:
                continue
            found_ips.add(ip)

            text = data.decode(errors='ignore')

            # Extract UUID
            uuid_match = re.search(r'Address>[^<]*(uuid:[^<]*)</', text)
            uuid = uuid_match.group(1) if uuid_match else ''
            # Normalize: strip 'uuid:' prefix for matching
            uuid_bare = uuid.replace('uuid:', '').strip()

            # Extract XAddrs
            xaddr_match = re.search(r'XAddrs>(.*?)</', text)
            xaddr = xaddr_match.group(1) if xaddr_match else ''

            # Extract scopes
            name = ''
            hardware = ''
            scopes_match = re.search(r'Scopes>(.*?)</', text)
            if scopes_match:
                for scope in scopes_match.group(1).split():
                    if '/name/' in scope:
                        name = unquote(scope.split('/name/')[-1])
                    elif '/hardware/' in scope:
                        hardware = unquote(scope.split('/hardware/')[-1])

            cam = {
                'ip': ip,
                'uuid': uuid_bare,
                'name': name,
                'hardware': hardware,
                'xaddr': xaddr,
            }
            cameras.append(cam)
            print(f"[INFO] Found camera: {name} ({hardware}) "
                  f"at {ip} uuid={uuid_bare}")
    except socket.timeout:
        pass
    sock.close()

    return cameras


def _parse_soap_fault(text):
    """Extract (reason_text, subcode) from a SOAP 1.2 fault response.

    Returns (None, None) if `text` isn't a parseable SOAP fault.
    Looks for both <env:Reason><env:Text>...</env:Text></env:Reason>
    (human-readable, what we want to surface) and
    <env:Code><env:Subcode><env:Value>...</env:Value></env:Subcode>
    (machine-readable, e.g. `ter:NotAuthorized`). Namespace-prefix
    agnostic because cameras use varying ones (env: / soap: / s:).
    """
    import re
    if 'Fault' not in text:
        return None, None
    reason_match = re.search(
        r'<[a-z0-9]+:Reason\b[^>]*>\s*<[a-z0-9]+:Text\b[^>]*>([^<]+)</[a-z0-9]+:Text>',
        text, re.IGNORECASE)
    subcode_match = re.search(
        r'<[a-z0-9]+:Subcode\b[^>]*>\s*<[a-z0-9]+:Value\b[^>]*>([^<]+)</[a-z0-9]+:Value>',
        text, re.IGNORECASE)
    return (
        reason_match.group(1).strip() if reason_match else None,
        subcode_match.group(1).strip() if subcode_match else None,
    )


def _onvif_ws_security_header(username, password):
    """Build WS-Security UsernameToken header for ONVIF authentication."""
    import hashlib
    import base64
    import os
    from datetime import datetime, timezone

    nonce_bytes = os.urandom(16)
    nonce_b64 = base64.b64encode(nonce_bytes).decode()
    created = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    digest_input = nonce_bytes + created.encode() + password.encode()
    digest = base64.b64encode(hashlib.sha1(digest_input).digest()).decode()

    return (
        '<soap:Header>'
        '<wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/'
        'oasis-200401-wss-wssecurity-secext-1.0.xsd"'
        ' xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/'
        'oasis-200401-wss-wssecurity-utility-1.0.xsd">'
        '<wsse:UsernameToken>'
        f'<wsse:Username>{username}</wsse:Username>'
        f'<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/'
        f'oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</wsse:Password>'
        f'<wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/'
        f'oasis-200401-wss-soap-message-security-1.0#Base64Binary">{nonce_b64}</wsse:Nonce>'
        f'<wsu:Created>{created}</wsu:Created>'
        '</wsse:UsernameToken>'
        '</wsse:Security>'
        '</soap:Header>'
    )


def _discover_media_service(device_url, auth_header='', timeout=5):
    """Query ONVIF device service for media service URL via GetServices."""
    import requests
    import re

    get_services = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"'
        ' xmlns:tds="http://www.onvif.org/ver10/device/wsdl">'
        f'{auth_header}'
        '<soap:Body><tds:GetServices><tds:IncludeCapability>false'
        '</tds:IncludeCapability></tds:GetServices></soap:Body></soap:Envelope>'
    )
    try:
        print(f"[INFO] Querying ONVIF GetServices at {device_url}")
        resp = requests.post(device_url, data=get_services,
                             headers={'Content-Type': 'application/soap+xml'},
                             timeout=timeout)
        if resp.status_code == 200:
            # Find media service XAddr
            # Match: <tds:XAddr>http://...</tds:XAddr> near media/wsdl namespace
            urls = re.findall(r'<[^:]*:?XAddr>([^<]*)</[^:]*:?XAddr>', resp.text)
            media_urls = [u for u in urls if 'media' in u.lower()]
            if media_urls:
                print(f"[INFO] Discovered media service URLs: {media_urls}")
                return media_urls
            print(f"[INFO] GetServices returned {len(urls)} services, none matched 'media': {urls}")
        else:
            print(f"[WARN] GetServices returned HTTP {resp.status_code}")
    except Exception as e:
        print(f"[WARN] GetServices failed: {e}")
    return []


def fetch_onvif_rtsp_urls(ip, username='', password='', xaddr='', timeout=5,
                          diagnostics=False):
    """Fetch RTSP stream URIs from an ONVIF camera via GetProfiles + GetStreamUri.
    Returns list of dicts with profile and url, or empty list on failure.
    With diagnostics=True, returns (urls, failure_code) so callers can
    distinguish rejected credentials from an unreachable camera.
    """
    import requests
    import re

    def finish(urls, failure_code=None):
        return (urls, failure_code) if diagnostics else urls

    from urllib.parse import urlparse
    if xaddr:
        parsed = urlparse(xaddr.split()[0])
        base = f"http://{parsed.hostname}:{parsed.port or 80}"
    else:
        base = f"http://{ip}"

    auth_header = _onvif_ws_security_header(username, password) if username else ''

    # Step 0: Query device service for media service URL via GetServices
    device_url = xaddr.split()[0] if xaddr else f"{base}/onvif/device_service"
    media_urls = _discover_media_service(device_url, auth_header, timeout)

    # Add common fallback paths
    for path in ['/onvif/media_service', '/onvif/Media', '/onvif/media']:
        url = f"{base}{path}"
        if url not in media_urls:
            media_urls.append(url)

    get_profiles = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"'
        ' xmlns:trt="http://www.onvif.org/ver10/media/wsdl">'
        f'{auth_header}'
        '<soap:Body><trt:GetProfiles/></soap:Body></soap:Envelope>'
    )

    # Try each URL until one works. STOP on auth failures - each
    # retry counts against the camera's brute-force lockout (Hik
    # locks after ~5 wrong attempts; iterating 5 paths trips it on
    # ONE wrong-password user click). 2026-05-27 incident.
    resp = None
    used_url = None
    auth_failed = False
    received_response = False
    for media_url in media_urls:
        try:
            print(f"[INFO] Trying ONVIF GetProfiles at {media_url}")
            resp = requests.post(media_url, data=get_profiles,
                                 headers={'Content-Type': 'application/soap+xml'},
                                 timeout=timeout)
            received_response = True
            print(f"[INFO] ONVIF GetProfiles {media_url}: HTTP {resp.status_code}")
            if resp.status_code == 200 and 'token="' in resp.text and 'Profiles' in resp.text:
                used_url = media_url
                break
            # Log response body. Parse SOAP fault to surface the actual
            # reason in plain English - cameras put the operator-
            # friendly message in <env:Reason><env:Text>, which the
            # raw XML dump used to hide behind a 500-char truncation.
            reason, subcode = _parse_soap_fault(resp.text)
            if reason:
                print(f"[WARN] ONVIF fault: {reason}"
                      + (f" (subcode={subcode})" if subcode else ""))
            elif resp.status_code != 200 or 'Fault' in resp.text:
                # Not a parseable SOAP fault (e.g. plain HTML 401 from
                # Hik with no creds) - dump the raw body, full size.
                print(f"[WARN] ONVIF response: {resp.text}")
            # Auth failure -> bail. Trying the other paths just wastes
            # lockout-counter strikes on the camera.
            is_auth_fault = (resp.status_code in (401, 403)
                             or (subcode and 'NotAuthorized' in subcode)
                             or (subcode and 'authenticate' in subcode.lower())
                             or (subcode and 'sender' in subcode.lower() and 'unauthorized' in (reason or '').lower()))
            if is_auth_fault:
                print(f"[ERROR] ONVIF authentication rejected by the camera; "
                      f"verify the camera username and password saved for "
                      f"this door. Stopping endpoint retries to avoid "
                      f"tripping the camera's brute-force lockout")
                auth_failed = True
                break
        except Exception as e:
            print(f"[WARN] ONVIF GetProfiles failed at {media_url}: {e}")

    if not used_url:
        if not auth_failed:
            print(f"[WARN] All ONVIF endpoints failed for {ip}")
        if auth_failed:
            return finish([], 'auth')
        if not received_response:
            return finish([], 'unreachable')
        return finish([], 'onvif')

    # Extract profile token + name pairs: <Profiles ... token="X"><Name>Y</Name>
    profile_pairs = re.findall(
        r'<[^:]*:?Profiles[^>]*token="([^"]*)"[^>]*>\s*<[^:]*:?Name>([^<]*)</',
        resp.text)
    if profile_pairs:
        profile_tokens = [p[0] for p in profile_pairs]
        profile_names = [p[1] for p in profile_pairs]
    else:
        profile_tokens = re.findall(r'Profiles\s[^>]*token="([^"]*)"', resp.text)
        profile_names = profile_tokens

    # Extract resolution per profile from VideoEncoderConfiguration
    profile_resolutions = {}
    profile_sections = re.split(r'Profiles\b[^>]*token="', resp.text)[1:]
    for section in profile_sections:
        token_match = re.match(r'([^"]*)"', section)
        if not token_match:
            continue
        tok = token_match.group(1)
        # Find resolution in VideoEncoderConfiguration > Resolution > Width/Height
        res_match = re.search(
            r'VideoEncoderConfiguration.*?Width>(\d+)<.*?Height>(\d+)<',
            section, re.DOTALL)
        enc_match = re.search(r'Encoding>([^<]*)</', section)
        if res_match:
            res = f"{res_match.group(1)}x{res_match.group(2)}"
            if enc_match:
                res += f" {enc_match.group(1)}"
            profile_resolutions[tok] = res

    print(f"[INFO] Found {len(profile_tokens)} profiles: {list(zip(profile_tokens, profile_names))}")
    if profile_resolutions:
        print(f"[INFO] Resolutions: {profile_resolutions}")

    if not profile_tokens:
        print(f"[WARN] No ONVIF profiles found for {ip}")
        return finish([], 'profiles')

    results = []
    for i, token in enumerate(profile_tokens):
        fresh_auth = _onvif_ws_security_header(username, password) if username else ''
        get_stream = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"'
            ' xmlns:trt="http://www.onvif.org/ver10/media/wsdl"'
            ' xmlns:tt="http://www.onvif.org/ver10/schema">'
            f'{fresh_auth}'
            '<soap:Body><trt:GetStreamUri>'
            '<trt:StreamSetup>'
            '<tt:Stream>RTP-Unicast</tt:Stream>'
            '<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport>'
            '</trt:StreamSetup>'
            f'<trt:ProfileToken>{token}</trt:ProfileToken>'
            '</trt:GetStreamUri></soap:Body></soap:Envelope>'
        )
        try:
            stream_resp = requests.post(used_url, data=get_stream,
                                 headers={'Content-Type': 'application/soap+xml'},
                                 timeout=timeout)
            uri_match = re.search(r'<[^:]*:?Uri>([^<]*)</[^:]*:?Uri>', stream_resp.text)
            if uri_match:
                # XML entities must be decoded: cameras correctly emit
                # `&amp;` between query params, but the raw regex capture
                # passes the literal `&amp;` straight into the RTSP URL.
                # Hikvision then sees `?transportmode=unicast&amp;profile=Profile_1`
                # as a single malformed param and the stream fails to open.
                import html
                rtsp_url = html.unescape(uri_match.group(1))
                name = profile_names[i] if i < len(profile_names) else token
                res = profile_resolutions.get(token, '')
                label = f"{name} ({res})" if res else name
                results.append({'profile': label, 'url': rtsp_url})
                print(f"[INFO] Camera {ip} profile '{label}': "
                      f"{video_decode.redact_rtsp_urls(rtsp_url)}")
            else:
                print(f"[WARN] No Uri found in GetStreamUri response for {token}")
        except Exception as e:
            print(f"[WARN] GetStreamUri failed for {ip} profile {token}: {e}")

    return finish(results, None if results else 'stream_uri')


def resolve_camera_address(camera_spec, discovered_cameras=None):
    """Resolve a camera specifier to an IP address.
    camera_spec can be:
      - Full RTSP URL: returned as-is
      - 'onvif:auto': first discovered camera
      - 'onvif:uuid:<uuid>': match by ONVIF UUID
      - 'onvif:<ip>': match by IP
      - 'onvif:<name>': match by ONVIF name/hardware
    """
    if not camera_spec.startswith('onvif:'):
        return camera_spec  # Already a full URL

    target = camera_spec[6:]  # after 'onvif:'
    if not discovered_cameras:
        print(f"[WARN] No ONVIF cameras discovered")
        return None

    if target == 'auto':
        cam = discovered_cameras[0]
        print(f"[INFO] Auto-selected camera: {cam['name']} at {cam['ip']}")
        return cam['ip']

    # Match by UUID (most reliable)
    uuid_target = target.replace('uuid:', '') if target.startswith('uuid:') \
        else None
    if uuid_target:
        for cam in discovered_cameras:
            if cam['uuid'] == uuid_target:
                print(f"[INFO] Matched UUID {uuid_target} -> "
                      f"{cam['name']} at {cam['ip']}")
                return cam['ip']
        print(f"[WARN] No camera with UUID '{uuid_target}' found")
        return None

    # Match by IP
    for cam in discovered_cameras:
        if target == cam['ip']:
            print(f"[INFO] Matched IP {target} -> {cam['name']}")
            return cam['ip']

    # Match by name or hardware (partial, case-insensitive)
    target_lower = target.lower()
    for cam in discovered_cameras:
        if (target_lower in cam['name'].lower() or
                target_lower in cam['hardware'].lower()):
            print(f"[INFO] Matched '{target}' -> "
                  f"{cam['name']} at {cam['ip']}")
            return cam['ip']

    print(f"[WARN] ONVIF camera '{target}' not found")
    return None


def _resolve_camera_details(camera_spec):
    """Re-discover a camera by UUID, preserving its ONVIF XAddr."""
    cameras = discover_onvif_cameras(timeout=3)
    ip = resolve_camera_address(camera_spec, cameras)
    if not ip:
        return None
    return next((camera for camera in cameras if camera['ip'] == ip),
                {'ip': ip, 'xaddr': ''})


def _inject_rtsp_credentials(url, username, password):
    """Insert user:pass@ into an RTSP URL if missing.

    Hikvision's GetStreamUri returns the bare `rtsp://host:port/path`
    even when RTSP requires authentication; OpenCV/FFmpeg then fail to
    open the stream. We hold the credentials anyway (same ones used
    for the ONVIF SOAP call), so splice them into the URL before
    handing it to the reader."""
    if not username:
        return url
    from urllib.parse import urlparse, urlunparse, quote
    parsed = urlparse(url)
    if parsed.username:
        return url  # already credentialed
    userinfo = quote(username, safe='')
    if password:
        userinfo += ':' + quote(password, safe='')
    host = parsed.hostname or ''
    if parsed.port:
        host += f':{parsed.port}'
    return urlunparse(parsed._replace(netloc=f'{userinfo}@{host}'))


def _fetch_onvif_session_url(camera_spec, username, password, profile='main'):
    """Fetch a fresh ONVIF session RTSP URL, re-discovering IP by UUID first."""
    camera = _resolve_camera_details(camera_spec)
    if not camera:
        print(f"[WARN] Could not discover camera for '{camera_spec}'")
        return None
    ip = camera['ip']
    urls, failure = fetch_onvif_rtsp_urls(
        ip, username=username, password=password,
        xaddr=camera.get('xaddr', ''), diagnostics=True)
    if failure == 'auth':
        raise CameraAuthenticationError(
            'camera rejected the saved ONVIF username or password')
    if not urls:
        return None
    profile_lower = profile.lower()
    profile_aliases = {
        'main': ('main', 'primary'),
        'sub': ('sub', 'minor', 'secondary'),
    }.get(profile_lower, (profile_lower,))
    chosen = None
    for u in urls:
        label = u['profile'].lower()
        if any(alias in label for alias in profile_aliases):
            chosen = u
            break
    if chosen is None:
        chosen = urls[0]
        print(f"[INFO] No profile match for '{profile}', using first ({ip})")
    final = _inject_rtsp_credentials(chosen['url'], username, password)
    # Mask the password in the log; the URL still appears in detector
    # logs that may be shared.
    safe = _inject_rtsp_credentials(chosen['url'], username, '***' if password else '')
    print(f"[INFO] Got ONVIF session URL for '{profile}' ({ip}): {safe}")
    return final


def build_rtsp_url(camera_spec, door_def, discovered_cameras=None):
    """Build the final RTSP URL from config, resolving onvif: and .local.
    If camera_path is /onvif/main or /onvif/sub, fetches a fresh ONVIF session URL.
    Returns (url, url_builder) where url_builder is a callable for session refresh
    or None for static URLs.
    """
    if camera_spec.startswith('onvif:'):
        ip = resolve_camera_address(camera_spec, discovered_cameras)
        if not ip:
            return None, None
        user = door_def.get('camera_user', 'admin')
        passwd = door_def.get('camera_pass', '')
        path = door_def.get('camera_path', '/stream2')
        port = door_def.get('camera_port', 554)
        builder = None

        # Special path: /onvif/main or /onvif/sub → fetch fresh session URL
        # Re-discovers camera IP by UUID on each call (handles IP changes)
        if path.startswith('/onvif/'):
            profile = path.split('/')[-1]  # 'main' or 'sub'
            auth_rejected = False

            def builder(_spec=camera_spec, _u=user, _p=passwd, _pr=profile):
                nonlocal auth_rejected
                if auth_rejected:
                    raise CameraAuthenticationError(
                        'camera previously rejected the saved credentials')
                try:
                    return _fetch_onvif_session_url(_spec, _u, _p, _pr)
                except CameraAuthenticationError:
                    auth_rejected = True
                    raise
            try:
                session_url = builder()
            except CameraAuthenticationError:
                print(f"[ERROR] Camera rejected the saved ONVIF credentials; "
                      f"the reader will remain paused until configuration "
                      f"changes")
                session_url = None
            if session_url:
                return session_url, builder
            print(f"[WARN] ONVIF session URL fetch failed, falling back to /stream2")
            path = '/stream2'

        url = _inject_rtsp_credentials(
            f"rtsp://{ip}:{port}{path}", user, passwd)
        return resolve_mdns_in_url(url), builder

    return resolve_mdns_in_url(camera_spec), None


def resolve_mdns_in_url(url):
    """Resolve .local mDNS hostnames in a URL to IP addresses.
    e.g. rtsp://user:pass@mycamera.local:554/stream -> rtsp://user:pass@192.168.1.5:554/stream
    """
    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname or not hostname.endswith('.local'):
        return url

    try:
        import socket
        ip = socket.getaddrinfo(hostname, None, socket.AF_INET)[0][4][0]
        # Rebuild netloc with resolved IP, preserving user:pass and port
        if parsed.username:
            userinfo = parsed.username
            if parsed.password:
                userinfo += f":{parsed.password}"
            netloc = f"{userinfo}@{ip}"
        else:
            netloc = ip
        if parsed.port:
            netloc += f":{parsed.port}"
        resolved = urlunparse(parsed._replace(netloc=netloc))
        print(f"[INFO] Resolved {hostname} -> {ip}")
        return resolved
    except Exception as e:
        print(f"[WARN] Could not resolve {hostname}: {e}")
        return url


# --- Camera stream reader thread ---

VIDEO_LOG_BUFFER_SECONDS = 5  # seconds of video before event
VIDEO_LOG_AFTER_SECONDS = 3   # seconds of video after event
CAMERA_RETRY_DELAYS_SECONDS = (2, 5, 10, 30, 60, 120, 300)
# Thumbnail picked from this far back in the pre-buffer. 2s lands on
# the person reaching the door instead of the QR-fills-frame moment.
THUMBNAIL_LOOKBACK_S = 2.0


class CameraReader(threading.Thread):
    """Reads frames from an RTSP stream in a background thread.
    Keeps the latest frame and a ring buffer for video logging.
    """

    def __init__(self, url, name="camera", buffer_seconds=VIDEO_LOG_BUFFER_SECONDS,
                 url_builder=None):
        super().__init__(daemon=True)
        self.url = url
        self._url_builder = url_builder
        self.name = name
        self._stopped = False
        self._stop_event = threading.Event()
        self._frame = None
        self._frame_ts = 0  # timestamp when frame was captured
        self._lock = threading.Lock()
        self._frame_count = 0
        self._fps = 15.0
        self._w = 0
        self._h = 0
        self._buffer_seconds = buffer_seconds
        self._ring_buffer = []  # list of (frame, timestamp)
        self._max_buffer = 150  # updated once FPS is known

    def run(self):
        global _running
        retry_index = 0
        while _running and not self._stopped:
            # Refresh URL if we have a builder (e.g. ONVIF session URLs)
            if self._url_builder:
                try:
                    fresh = self._url_builder()
                except CameraAuthenticationError:
                    print(f"[ERROR] [{self.name}] Camera rejected the saved "
                          f"ONVIF credentials. Automatic retries are paused "
                          f"until the door configuration changes.")
                    self._stop_event.wait()
                    continue
                if fresh:
                    self.url = fresh
            # Pick the decode backend (nvdec/vaapi/cpu); hardware paths
            # fall back to cpu cleanly inside open_reader.
            try:
                reader = video_decode.open_reader(
                    self.url, VIDEO_DECODE_BACKEND, self.name)
            except video_decode.StreamAuthenticationError:
                print(f"[ERROR] [{self.name}] Camera rejected the saved RTSP "
                      f"credentials. Automatic retries are paused until the "
                      f"door configuration changes.")
                self._stop_event.wait()
                continue
            if reader is None:
                delay = CAMERA_RETRY_DELAYS_SECONDS[retry_index]
                retry_index = min(
                    retry_index + 1,
                    len(CAMERA_RETRY_DELAYS_SECONDS) - 1)
                print(f"[WARN] [{self.name}] Cannot open RTSP stream; "
                      f"see the preceding probe error for the cause. "
                      f"Retrying in {delay}s...")
                self._stop_event.wait(delay)
                continue

            self._w = reader.width
            self._h = reader.height
            self._fps = reader.fps or 15.0
            self._max_buffer = int(self._fps * self._buffer_seconds) + 10

            while _running and not self._stopped:
                ret, frame = reader.read()
                if not ret:
                    delay = CAMERA_RETRY_DELAYS_SECONDS[retry_index]
                    retry_index = min(
                        retry_index + 1,
                        len(CAMERA_RETRY_DELAYS_SECONDS) - 1)
                    print(f"[WARN] [{self.name}] Frame read failed, "
                          f"reconnecting in {delay}s...")
                    break
                # A real frame proves the stream recovered. Future failures
                # should retry promptly again.
                retry_index = 0
                now = time.time()
                with self._lock:
                    self._frame = frame
                    self._frame_ts = now
                    self._frame_count += 1
                    self._ring_buffer.append((frame, now))
                    if len(self._ring_buffer) > self._max_buffer:
                        self._ring_buffer = \
                            self._ring_buffer[-self._max_buffer:]

            reader.release()
            if _running and not self._stopped:
                self._stop_event.wait(delay)

    def get_frame(self):
        """Get the latest frame. Returns (frame, frame_count, capture_ts)
        or (None, 0, 0)."""
        with self._lock:
            return self._frame, self._frame_count, self._frame_ts

    def get_buffer_snapshot(self):
        """Get a copy of the current ring buffer for video logging."""
        with self._lock:
            return list(self._ring_buffer), self._fps, self._w, self._h

    def stop(self):
        """Signal this reader to stop."""
        self._stopped = True
        self._stop_event.set()


class VideoLogger:
    """Saves short video clips on access events in a background thread."""

    def __init__(self, base_dir, after_seconds=VIDEO_LOG_AFTER_SECONDS):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.after_seconds = after_seconds

    def log_event(self, door_name, event_type, token, confidence,
                  camera_reader):
        """Start background recording. Always records — parallel recordings are safe."""
        t = threading.Thread(
            target=self._record,
            args=(door_name, event_type, token, confidence, camera_reader),
            daemon=True,
        )
        t.start()
        return True

    def _record(self, door_name, event_type, token, confidence,
                camera_reader):
        ts = datetime.now().strftime('%Y-%m-%d-%H-%M-%S')
        safe_name = door_name.replace(' ', '_')
        dirname = f"{event_type}-{ts}"
        event_dir = self.base_dir / safe_name / dirname
        event_dir.mkdir(parents=True, exist_ok=True)

        # Save token info
        token_file = event_dir / "token.txt"
        token_file.write_text(
            f"token: {token}\n"
            f"event: {event_type}\n"
            f"door: {door_name}\n"
            f"time: {ts}\n"
            f"confidence: {confidence:.2f}\n"
        )

        # Get frames before the event
        pre_frames, fps, w, h = camera_reader.get_buffer_snapshot()
        if not pre_frames or w == 0 or h == 0:
            return

        # Collect frames after the event
        post_frames = []
        after_count = int(fps * self.after_seconds)
        t_start = time.time()
        while (len(post_frames) < after_count and
               time.time() - t_start < self.after_seconds + 2):
            frame, _, _ = camera_reader.get_frame()
            if frame is not None:
                post_frames.append(frame)
            time.sleep(1.0 / fps)

        # Write video (temp file with mp4v, then re-encode to H.264)
        tmp_path = str(event_dir / "video_raw.mp4")
        video_path = str(event_dir / "video.mp4")
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        writer = cv2.VideoWriter(tmp_path, fourcc, fps, (w, h))

        for frame, _ in pre_frames:
            writer.write(frame)
        for frame in post_frames:
            writer.write(frame)

        writer.release()
        total = len(pre_frames) + len(post_frames)

        # Save a JPG thumbnail from ~THUMBNAIL_LOOKBACK_S before the
        # decode moment. The newest pre_frame is right when the QR fills
        # the camera (basically a photo of a QR code, useless for "who
        # was this?"); walking back a couple seconds lands on the
        # person approaching the door, which is what the operator
        # actually wants to see in the access log. Falls back through
        # the buffer if it's partially filled (just-booted detector)
        # and finally to first post_frame.
        try:
            thumb_frame = None
            if pre_frames:
                newest_ts = pre_frames[-1][1] or 0
                thumb_frame = pre_frames[0][0]  # earliest available
                for frame, ts in reversed(pre_frames):
                    if ts and newest_ts - ts >= THUMBNAIL_LOOKBACK_S:
                        thumb_frame = frame
                        break
            elif post_frames:
                thumb_frame = post_frames[0]
            if thumb_frame is not None:
                cv2.imwrite(str(event_dir / 'thumbnail.jpg'),
                            thumb_frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
        except Exception as e:
            print(f"[WARN] [{door_name}] thumbnail save failed: {e}")

        # Re-encode to H.264 for browser playback (try NVENC GPU, fallback CPU)
        import subprocess
        encoded = False
        # nvenc is GPU-only: on a CPU-only host it fails every event and
        # wastes a process spawn, so skip straight to libx264 there.
        codecs = ([('h264_nvenc', 'p1'), ('libx264', 'ultrafast')]
                  if HAS_GPU else [('libx264', 'ultrafast')])
        for codec, preset in codecs:
            try:
                result = subprocess.run([
                    'ffmpeg', '-y', '-i', tmp_path,
                    '-c:v', codec, '-preset', preset,
                    '-movflags', '+faststart',
                    video_path,
                ], capture_output=True, timeout=30)
                if result.returncode == 0 and os.path.exists(video_path):
                    os.remove(tmp_path)
                    encoded = True
                    print(f"[LOG] [{door_name}] Encoded with {codec}")
                    break
                else:
                    stderr = result.stderr.decode(errors='ignore')[-200:]
                    print(f"[WARN] [{door_name}] {codec} failed: {stderr}")
            except Exception as e:
                print(f"[WARN] [{door_name}] {codec} error: {e}")
        if not encoded:
            os.rename(tmp_path, video_path)
            print(f"[WARN] [{door_name}] Using raw mpeg4 (re-encode failed)")

        print(f"[LOG] [{door_name}] Saved {total} frames to {event_dir}")


# --- Door processing config ---

class DoorConfig:
    """Configuration for a single door."""

    def __init__(self, name, camera_url, door_controller, valid_tokens,
                 url_builder=None, open_seconds=5):
        self.name = name
        self.camera_url = camera_url
        self.door = door_controller
        self.valid_tokens = set(valid_tokens)
        self.url_builder = url_builder  # callable for ONVIF session refresh
        # Beep/open length; also the same-token re-grant cooldown so a
        # repeat scan re-fires as soon as the previous open finishes.
        self.open_seconds = open_seconds
        self.camera = None  # set after starting
        self.token_count = 0
        self.door_opens = 0
        self.denied_count = 0
        self._last_processed_frame = 0
        self._last_processed_time = 0.0  # for time-based fps throttle
        # Per-door median-stacking decoder (recovers hard-to-read QRs from
        # recent frames). One per door so concurrent doors don't pollute
        # each other's stacks; it also resets between visitors so one
        # person's frames can't stack into another's decode. Lazily built
        # in run_multi_door once decode_fn is available.
        self.stacker = None
        # Latest detection state for the /api/doors/<name>/live MJPEG
        # preview. List of (x1, y1, x2, y2, conf, token_or_None) tuples
        # plus a wall-clock timestamp; the live endpoint draws these as
        # an overlay on the most recent CameraReader frame. Updated
        # under _live_lock from the main detect loop so a separate
        # MJPEG-serving thread can read them without tearing.
        self.latest_detections = []
        self.latest_detections_ts = 0.0
        self._live_lock = threading.Lock()
        # Per-region tracks for decode-latency measurement (the
        # user-perceived "how long did I stand at the door" number).
        # Each detected region carries its own first-seen / first-
        # decoded timestamps; a grant reports the latency of the track
        # that decoded. Replaces the old single burst marker, which a
        # persistent QR-shaped object in the scene (laptop screen,
        # poster) kept armed forever - inflating decode_ms to "time
        # since previous grant" (observed: 9-40s for a 0.2s decode).
        self.tracks = QrTracks()


# Module-level registry of running DoorConfigs keyed by door name. The
# web layer reads this to find the right CameraReader + detection
# state when serving the /api/doors/<name>/live MJPEG stream. Updated
# by run_multi_door on startup; entries persist until the process
# exits (a config reload kills the whole detector loop so the registry
# rebuilds from scratch on restart).
_LIVE_DOORS = {}
_LIVE_DOORS_LOCK = threading.Lock()


def get_live_door(name):
    """Return the running DoorConfig for `name`, or None if not running.
    Web layer uses this to expose a live preview of each door's camera."""
    with _LIVE_DOORS_LOCK:
        return _LIVE_DOORS.get(name)


def list_live_doors():
    """Return a list of (name, DoorConfig) for currently running doors."""
    with _LIVE_DOORS_LOCK:
        return list(_LIVE_DOORS.items())


# --- Main loop ---

# Same-token re-grant cooldown is per-door = its open_seconds (beep
# length): a held-up QR can't re-fire while the door is still open, but a
# fresh present right after the beep re-grants immediately. See
# DoorConfig.open_seconds and the per-token check below.


def run_multi_door(doors, detector, decode_fn, conf=0.3, target_fps=5.0,
                   video_logger=None):
    """Main loop: process all doors, sharing the detector for YOLO.

    Detection is throttled to `target_fps` per door using a time-based gate
    (independent of the camera's stream fps; naturally capped by detector
    throughput on slow hosts). target_fps<=0 means every frame."""
    min_interval = (1.0 / target_fps) if target_fps and target_fps > 0 else 0.0
    global _running, _camera_readers

    # Stop any leftover camera readers from previous run
    for r in _camera_readers:
        r.stop()
    _camera_readers.clear()

    # Cooldown tracker: (door_name, token) -> last_event_time
    last_event = {}

    # Start camera reader threads
    for door_cfg in doors:
        door_cfg.camera = CameraReader(door_cfg.camera_url, door_cfg.name,
                                       url_builder=door_cfg.url_builder)
        door_cfg.camera.start()
        _camera_readers.append(door_cfg.camera)

    # Publish this run's doors to the module-level registry so the web
    # layer (Flask thread, same process) can serve live previews. Wipe
    # any leftover entries from a prior run; the new dict matches the
    # set of CameraReaders we just spawned. Lock guards readers in the
    # MJPEG generator from seeing a partially-rebuilt map.
    with _LIVE_DOORS_LOCK:
        _LIVE_DOORS.clear()
        for door_cfg in doors:
            _LIVE_DOORS[door_cfg.name] = door_cfg

    # Wait for first frames
    time.sleep(2)

    t_start = time.perf_counter()
    last_status = t_start
    total_processed = 0
    interval_processed = 0  # frames since last status print

    print(f"\n[INFO] Processing {len(doors)} door(s) "
          f"(target_fps={target_fps}, conf={conf})")
    print(f"[INFO] Press Ctrl+C to stop\n")

    while _running:
        any_frame = False

        for door_cfg in doors:
            frame, frame_count, capture_ts = door_cfg.camera.get_frame()
            if frame is None:
                continue
            if frame_count == door_cfg._last_processed_frame:
                continue  # already processed this frame
            # Throttle detection to target_fps (time-based -> independent of
            # the camera's stream fps). Don't advance _last_processed_frame
            # while throttled, so we pick up the newest frame once the
            # interval elapses.
            now_mono = time.monotonic()
            if now_mono - door_cfg._last_processed_time < min_interval:
                continue

            door_cfg._last_processed_frame = frame_count
            door_cfg._last_processed_time = now_mono
            frame_lag = time.time() - capture_ts if capture_ts else 0
            any_frame = True
            total_processed += 1
            interval_processed += 1

            # YOLO detection on the active backend (cpu/igpu/gpu)
            detections = detector.detect(frame, conf=conf)

            # Assign each detection to a per-region track; decode
            # latency is measured per track (first_seen ->
            # first_decoded), so a persistent non-decoding region in
            # the scene can't inflate another region's number.
            # capture_ts is the frame's wall-clock capture timestamp,
            # so the latency is true end-to-end "QR visible -> token
            # decoded" duration (not just CPU time).
            now_wallclock = time.time()
            track_ids = door_cfg.tracks.update(
                detections, capture_ts or now_wallclock)

            # Per-frame snapshot of detections + their first decoded
            # token. Published to door_cfg.latest_detections so the
            # /api/doors/<name>/live MJPEG endpoint can draw an overlay
            # on the latest CameraReader frame. Updated under the lock
            # so the live thread doesn't see torn writes.
            live_dets = []

            for det_idx, (x1, y1, x2, y2, confidence) in enumerate(detections):
                crop = frame[y1:y2, x1:x2]
                if crop.size == 0:
                    continue

                if door_cfg.stacker is None:
                    door_cfg.stacker = StackingDecoder(decode_fn)
                tokens, _ = door_cfg.stacker.decode(crop, now_wallclock)
                if tokens:
                    # Stamp the track's first successful decode; repeat
                    # decodes of a held-up phone keep the original
                    # stamp, so repeat grants report the original
                    # presentation latency instead of the cooldown.
                    door_cfg.tracks.mark_decoded(
                        track_ids[det_idx], capture_ts or now_wallclock)
                live_dets.append(
                    (int(x1), int(y1), int(x2), int(y2),
                     float(confidence),
                     tokens[0] if tokens else None))

                for token in tokens:
                    # Cooldown: skip if same door+token granted within the
                    # door's open window (beep length). Matches the relay
                    # controller's cooldown so a repeat scan re-fires as
                    # soon as the previous open finishes - not before.
                    now_t = time.time()
                    event_key = (door_cfg.name, token)
                    if now_t - last_event.get(event_key, 0) < door_cfg.open_seconds:
                        continue
                    last_event[event_key] = now_t

                    decode_ms = door_cfg.tracks.latency_ms(
                        track_ids[det_idx])

                    door_cfg.token_count += 1
                    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

                    # Resolve the scanned payload (short_id alias or full
                    # token, depending on whether the QR was minted before
                    # or after the alias migration) to the canonical full
                    # token so audit + per-user joins stay stable. Falls
                    # back to the door's static yaml list when DB is
                    # unreachable.
                    try:
                        from web.db import get_token_by_payload, get_all_active_tokens
                        row = get_token_by_payload(token)
                        if row:
                            is_valid = bool(row.get('active', 1))
                            token = row['token']  # rewrite to canonical
                        else:
                            is_valid = token in get_all_active_tokens()
                    except Exception:
                        is_valid = token in door_cfg.valid_tokens

                    decode_str = f" decode={decode_ms}ms" if decode_ms is not None else ""
                    # Relay reaction: grant decision -> relay command
                    # confirmed (HTTP response from the Shelly). Includes
                    # any resolve-wait inside open(), so it reflects the
                    # user-perceived gap between "scanner saw me" and
                    # "strike buzzed". None when no command was sent
                    # (dry-run, door cooldown, or relay unreachable).
                    relay_ms = None
                    if is_valid:
                        event = "ACCESS-GRANTED"
                        print(f"[{ts}] [{door_cfg.name}] ACCESS GRANTED: "
                              f"{token} (frame={frame_count}, "
                              f"conf={confidence:.2f}, "
                              f"lag={frame_lag:.1f}s{decode_str})")
                        if door_cfg.door:
                            relay_t0 = time.monotonic()
                            if door_cfg.door.open(token):
                                relay_ms = int(
                                    (time.monotonic() - relay_t0) * 1000)
                                door_cfg.door_opens += 1
                        else:
                            print(f"[{ts}] [{door_cfg.name}] DRY RUN: "
                                  f"would open door")
                        # Restart this region's decode-latency measurement
                        # so the next grant of a parked/held code reports
                        # its own cycle's latency, not the frozen first one.
                        door_cfg.tracks.reset_after_grant(
                            track_ids[det_idx], capture_ts or now_wallclock)
                    else:
                        event = "ACCESS-DENIED"
                        door_cfg.denied_count += 1
                        print(f"[{ts}] [{door_cfg.name}] ACCESS DENIED: "
                              f"{token} (frame={frame_count}, "
                              f"conf={confidence:.2f}, "
                              f"lag={frame_lag:.1f}s{decode_str})")

                    # Try to record video (may be skipped by cooldown).
                    # Predict both video + thumbnail relative paths; the
                    # background recorder writes them under the same
                    # event_dir, and the dashboard's <img> + Play link
                    # both fall back to the legacy bare button when null.
                    video_rel = None
                    thumb_rel = None
                    if video_logger:
                        video_ts = datetime.now().strftime('%Y-%m-%d-%H-%M-%S')
                        safe_name = door_cfg.name.replace(' ', '_')
                        evdir = f"{safe_name}/{event}-{video_ts}"
                        video_rel = f"{evdir}/video.mp4"
                        thumb_rel = f"{evdir}/thumbnail.jpg"
                        if not video_logger.log_event(
                                door_cfg.name, event, token,
                                confidence, door_cfg.camera):
                            video_rel = None  # cooldown - no video recorded
                            thumb_rel = None

                    # Log to database and push to event bus
                    try:
                        from web.db import log_access
                        log_id = log_access(door_cfg.name, token, event,
                                            video_path=video_rel,
                                            thumbnail_path=thumb_rel,
                                            decode_ms=decode_ms,
                                            relay_ms=relay_ms)
                        # Push to SSE clients via event bus
                        from web.events import event_bus
                        from web.db import get_access_logs_since
                        entries = get_access_logs_since(log_id - 1)
                        for entry in entries:
                            if entry['id'] == log_id:
                                event_bus.publish(entry)
                    except Exception as e:
                        print(f"[ERROR] Failed to log/publish event: {e}")

            # Publish this frame's detections (could be empty) so the
            # live MJPEG endpoint can render an up-to-date overlay.
            # Always update the timestamp, even on empty: the live
            # endpoint stale-outs detections older than ~1s, so an
            # empty publish here clears the previous frame's boxes
            # rather than leaving them stuck on screen.
            with door_cfg._live_lock:
                door_cfg.latest_detections = live_dets
                door_cfg.latest_detections_ts = time.time()

        if not any_frame:
            time.sleep(0.01)  # avoid busy loop when no new frames

        # Check for config reload signal from web UI
        if _reload_event and _reload_event.is_set():
            print(f"[INFO] Reload signal received, stopping detector...")
            break

        now = time.perf_counter()
        if now - last_status >= 10.0:
            interval_elapsed = now - last_status
            fps = interval_processed / interval_elapsed if interval_elapsed > 0 else 0
            parts = []
            for d in doors:
                # Measure current frame lag
                _, _, cts = d.camera.get_frame()
                lag = time.time() - cts if cts else 0
                parts.append(f"{d.name}: tokens={d.token_count} "
                             f"opens={d.door_opens} denied={d.denied_count} "
                             f"lag={lag:.1f}s")
            print(f"[STATUS] {total_processed} processed, "
                  f"{fps:.1f} FPS | "
                  f"{' | '.join(parts)}")
            DETECTOR_INFO['fps'] = round(fps, 1)
            # Reflect the backend a stream actually decoded with (a failed
            # hardware path falls back to cpu).
            active = video_decode.get_active()
            if active:
                DETECTOR_INFO['video_decode'] = active
            interval_processed = 0
            last_status = now

            # Write status file for web UI camera status display
            try:
                status = {}
                for d in doors:
                    last_frame_ts = None
                    if d.camera:
                        buf, _, _, _ = d.camera.get_buffer_snapshot()
                        if buf:
                            last_frame_ts = buf[-1][1]  # timestamp of latest frame
                    status[d.name] = {
                        'last_frame_ts': last_frame_ts,
                        'fps': fps,
                    }
                with open('config/detector_status.json', 'w') as f:
                    json.dump(status, f)
            except Exception:
                pass

    elapsed = time.perf_counter() - t_start
    print(f"\n[DONE] {total_processed} frames in {elapsed:.1f}s "
          f"({total_processed/elapsed:.1f} FPS)")
    for d in doors:
        print(f"  [{d.name}] tokens={d.token_count} opens={d.door_opens} "
              f"denied={d.denied_count}")


# --- Config loading ---

def load_config(config_path):
    """Load YAML config file."""
    with open(config_path) as f:
        return yaml.safe_load(f)


def generate_sample_config(path):
    """Generate a sample config file."""
    sample = {
        'doors': [
            {
                'name': 'Front Door',
                'camera': 'rtsp://<user>:<pass>@<camera-host>:554/stream2',
                'shelly': 'auto',
                'shelly_pass': 'your_password',
                'open_seconds': 5,
                'tokens': ['6f9c2a8d3b1e4f7a9c0d2e5f8a1b3c4d'],
            },
            {
                'name': 'Back Door',
                'camera': 'rtsp://<user>:<pass>@<camera-host>:554/stream2',
                'shelly': 'shelly1minig3-aabbccddeeff',
                'shelly_pass': 'another_password',
                'open_seconds': 3,
                'tokens': [
                    '6f9c2a8d3b1e4f7a9c0d2e5f8a1b3c4d',
                    'aaaa1111bbbb2222cccc3333dddd4444',
                ],
            },
        ],
    }
    with open(path, 'w') as f:
        yaml.dump(sample, f, default_flow_style=False, sort_keys=False)
    print(f"[INFO] Sample config written to {path}")


def main():
    parser = argparse.ArgumentParser(
        description='Live RTSP QR code detector with multi-door control')
    parser.add_argument('--config', type=str, default=None,
                        help='YAML config file for multi-door setup')
    parser.add_argument('--generate-config', type=str, default=None,
                        metavar='PATH',
                        help='Generate sample config file and exit')
    # Single-door CLI args (used when no config file)
    parser.add_argument('--url', default=None,
                        help='RTSP stream URL (single-door mode)')
    parser.add_argument('--shelly-ip', default=None,
                        help='Shelly device IP (single-door mode)')
    parser.add_argument('--open-seconds', type=int, default=5,
                        help='Door open duration (default: 5)')
    parser.add_argument('--shelly-pass', default=None,
                        help='Shelly admin password (single-door mode)')
    parser.add_argument('--token', action='append',
                        help='Valid token (repeatable, single-door mode)')
    # Shared args
    parser.add_argument('--target-fps', type=float,
                        default=float(os.environ.get('TARGET_FPS', '5')),
                        help='Max detections/sec per door; env TARGET_FPS '
                             'overrides. Time-based, so independent of the '
                             'camera stream fps (capped by detector '
                             'throughput). 0 = every frame. Default 5.')
    parser.add_argument('--model-size', default='auto',
                        choices=['auto', 'n', 's', 'm', 'l'],
                        help='YOLO model size; auto = n on CPU, s on GPU')
    parser.add_argument('--pipeline-backend',
                        default=os.environ.get('PIPELINE_BACKEND', 'auto'),
                        choices=['auto', 'cpu', 'igpu', 'gpu'],
                        help='Decode+detect backend; env PIPELINE_BACKEND '
                             'overrides. auto = gpu on NVIDIA, igpu on an '
                             'Intel/AMD iGPU, else cpu. Sets the decode '
                             'backend unless --decode-backend overrides.')
    parser.add_argument('--decode-backend',
                        default=os.environ.get('DECODE_BACKEND', 'auto'),
                        choices=['auto', 'nvdec', 'vaapi', 'cpu'],
                        help='Override the video decode backend; auto = '
                             'derive from --pipeline-backend.')
    parser.add_argument('--conf', type=float, default=0.3,
                        help='Detection confidence threshold (default: 0.3)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Do not trigger door relays')
    parser.add_argument('--video-log-dir', default='video-logs',
                        help='Directory for access event video logs '
                             '(default: video-logs)')
    args = parser.parse_args()

    if args.generate_config:
        generate_sample_config(args.generate_config)
        return

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Load config
    if args.config:
        config = load_config(args.config)
        print(f"[INFO] Loaded config: {args.config} "
              f"({len(config['doors'])} door(s))")
    else:
        # Build single-door config from CLI args
        url = args.url or DEFAULT_CONFIG['doors'][0]['camera']
        tokens = args.token or DEFAULT_CONFIG['doors'][0]['tokens']
        door_cfg = {
            'name': 'Door',
            'camera': url,
            'shelly': args.shelly_ip or 'auto',
            'open_seconds': args.open_seconds,
            'tokens': tokens,
        }
        if args.shelly_pass:
            door_cfg['shelly_pass'] = args.shelly_pass
        config = {'doors': [door_cfg]}

    # Discover Shelly devices if needed
    need_shelly_discovery = any(
        d.get('shelly', 'auto') != 'auto' and
        not all(p.isdigit() for p in d.get('shelly', '').split('.'))
        or d.get('shelly', 'auto') == 'auto'
        for d in config['doors']
    )
    discovered_shelly = {}
    if need_shelly_discovery and not args.dry_run:
        discovered_shelly = discover_shelly(timeout=5)

    # Discover ONVIF cameras if needed
    need_onvif = any(
        d.get('camera', '').startswith('onvif:')
        for d in config['doors']
    )
    discovered_cameras = []
    if need_onvif:
        discovered_cameras = discover_onvif_cameras(timeout=3)

    # Build door configs
    doors = []
    for door_def in config['doors']:
        name = door_def['name']
        camera_url, url_builder = build_rtsp_url(door_def['camera'], door_def,
                                                 discovered_cameras)
        if not camera_url:
            print(f"[ERROR] [{name}] Could not resolve camera, skipping")
            continue
        valid_tokens = set(door_def.get('tokens', []))
        open_seconds = door_def.get('open_seconds', 5)
        shelly_pass = door_def.get('shelly_pass', None)

        if args.dry_run:
            door_ctrl = None
            print(f"[INFO] [{name}] DRY RUN mode")
        elif door_def.get('opener', 'shelly') == 'onvif':
            # No-Shelly door: drive the camera's own ONVIF alarm relay.
            cam_ip = resolve_camera_address(door_def['camera'],
                                            discovered_cameras)
            if cam_ip:
                camera = next(
                    (candidate for candidate in discovered_cameras
                     if candidate['ip'] == cam_ip), {})
                door_ctrl = OnvifRelayController(
                    name, cam_ip,
                    username=door_def.get('camera_user', ''),
                    password=door_def.get('camera_pass', ''),
                    open_seconds=open_seconds,
                    relay_token=door_def.get('relay_token', 'auto'),
                    device_url=camera.get('xaddr', ''))
                print(f"[INFO] [{name}] Opener: ONVIF camera relay at "
                      f"{cam_ip} (relay={door_ctrl.relay_token or 'auto'})")
            else:
                print(f"[WARN] [{name}] ONVIF opener: camera not resolved")
                door_ctrl = None
        else:
            shelly_spec = door_def.get('shelly', 'auto')
            if shelly_spec == 'auto':
                # 'auto' = first device the startup discovery found.
                # No retry semantics for this case (resolving 'auto' on
                # the fly would change which device a door talks to,
                # which would be surprising).
                shelly_spec = resolve_shelly_address('auto', discovered_shelly)
            if shelly_spec:
                door_ctrl = DoorController(name, shelly_spec, open_seconds,
                                           password=shelly_pass)
                # Discovery now happens lazily in a background worker
                # inside DoorController. Just print intent here; the
                # worker logs the actual resolved IP when it lands.
                print(f"[INFO] [{name}] Shelly '{shelly_spec}' "
                      f"(auth={'yes' if shelly_pass else 'no'}, "
                      f"resolution: lazy via mDNS)")
            else:
                print(f"[WARN] [{name}] No Shelly configured ('auto' "
                      f"requested but none discovered)")
                door_ctrl = None

        print(f"[INFO] [{name}] Camera: "
              f"{video_decode.redact_rtsp_urls(camera_url)}")
        print(f"[INFO] [{name}] Valid tokens: {len(valid_tokens)}")

        doors.append(DoorConfig(name, camera_url, door_ctrl, valid_tokens,
                               url_builder=url_builder,
                               open_seconds=open_seconds))

    # Resolve 'auto' model size from the one GPU switch: nano on CPU-only
    # hosts (keeps up with the frame rate), small on GPU (full accuracy).
    if args.model_size == 'auto':
        args.model_size = 's' if HAS_GPU else 'n'

    # Resolve the pipeline backend (cpu/igpu/gpu): pairs the video decode
    # backend with the YOLO detector backend so decode + detect run on the
    # same device. The decode backend can still be overridden explicitly.
    pipeline_backend = pipeline.detect_pipeline_backend(args.pipeline_backend)
    global VIDEO_DECODE_BACKEND
    if args.decode_backend == 'auto':
        VIDEO_DECODE_BACKEND = pipeline.decode_backend_for(pipeline_backend)
    else:
        VIDEO_DECODE_BACKEND = args.decode_backend

    # Build the detector for the backend; falls back to PyTorch-CPU on any
    # hardware/export failure (never takes a door offline).
    detector, actual_backend = pipeline.build_detector(
        pipeline_backend, args.model_size)
    decode_fn = create_decoder()

    di = detector.info()
    print(f"[INFO] pipeline={actual_backend} -> decode={VIDEO_DECODE_BACKEND}, "
          f"detect={di['detect']}@{di['detect_device']}, "
          f"model=YOLOv8-{args.model_size}, "
          f"video encoder={'h264_nvenc->libx264' if HAS_GPU else 'libx264'}")

    DETECTOR_INFO.update({
        'gpu': HAS_GPU,
        'gpu_name': _gpu_name(),
        'cpu_name': _cpu_name(),
        'pipeline': actual_backend,
        'backend': di['detect'],
        'detect_device': di['detect_device'],
        'model': args.model_size,
        'encoder': 'h264_nvenc->libx264' if HAS_GPU else 'libx264',
        # Detected decode backend; refreshed to the actually-used one
        # (which may fall back to cpu) by run_multi_door once a stream opens.
        'video_decode': VIDEO_DECODE_BACKEND,
        'fps': None,
    })

    video_logger = VideoLogger(args.video_log_dir)
    print(f"[INFO] Video logs: {args.video_log_dir}")

    run_multi_door(doors, detector, decode_fn,
                   conf=args.conf, target_fps=args.target_fps,
                   video_logger=video_logger)


if __name__ == "__main__":
    main()
