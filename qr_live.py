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

def discover_shelly(timeout=5):
    """Auto-discover Shelly devices on the local network via mDNS.
    Returns dict: shelly_id -> ip  (e.g. 'shelly1minig3-aabbccddeeff' -> '10.0.0.5')
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

    return discovered


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
    """Controls door strike via Shelly 1 Mini Gen3 relay."""

    def __init__(self, name, shelly_ip, open_seconds=5, password=None):
        self.name = name
        self.shelly_ip = shelly_ip
        self.open_seconds = open_seconds
        self._auth = HTTPDigestAuth("admin", password) if password else None
        self._last_open = 0
        self._cooldown = open_seconds + 2
        self._lock = threading.Lock()
        self._session = requests.Session()
        if self._auth:
            self._session.auth = self._auth

    def _get(self, url):
        return self._session.get(url, timeout=3)

    def open(self, token):
        """Open door strike. Returns True if command was sent."""
        with self._lock:
            now = time.time()
            if now - self._last_open < self._cooldown:
                return False
            self._last_open = now

        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        url = (f"http://{self.shelly_ip}/rpc/Switch.Set"
               f"?id=0&on=true&toggle_after={self.open_seconds}")
        try:
            resp = self._get(url)
            resp.raise_for_status()
            print(f"[{ts}] [{self.name}] DOOR OPEN for "
                  f"{self.open_seconds}s "
                  f"(token={token[:8]}...) shelly={resp.text}")
            return True
        except Exception as e:
            print(f"[{ts}] [{self.name}] DOOR ERROR: {e}")
            return False

    def status(self):
        url = f"http://{self.shelly_ip}/rpc/Switch.GetStatus?id=0"
        try:
            resp = self._get(url)
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            return f"error: {e}"


# --- YOLO + QR decode ---

def get_tensorrt_engine_path(model_size='s'):
    cache_dir = Path(os.environ.get('MODEL_CACHE', '/tmp/qr-models'))
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"qrdet_{model_size}.engine"


def create_detector(model_size='s'):
    if HAS_ULTRALYTICS:
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


# Frame-stacking ring depth. On per-frame decode miss we median-stack the
# last STACK_N crops (aligned to the current crop shape) and retry the
# preprocess+decode pipeline. Targets rolling-shutter horizontal bands
# that shift between frames when an IP camera films a phone screen - the
# bands' position drifts as camera readout and screen PWM phase walk past
# each other, so a median across N frames cancels them out. Benchmarked
# on a banded video: lifted decode rate from 1.4% to 38% and time-to-
# first-decode from ~3.4s to ~0.6s. On clean video the path never
# engages (single-frame decode succeeds first), so no happy-path cost.
STACK_N = int(os.environ.get('REEFY_QR_STACK_N', '5'))


def _resize_to(img, shape):
    h, w = shape[:2]
    if img.shape[:2] == (h, w):
        return img
    return cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)


def decode_with_stacking(decode_fn, crop, crop_ring):
    """Single-frame decode first; on miss, median-stack the prior crops
    aligned to the current crop and retry. Returns (tokens, used_stack)
    where `used_stack` is True if stacking is what produced the tokens
    (used for telemetry / debugging).

    `crop_ring` is a per-camera deque(maxlen=STACK_N); the caller is
    responsible for holding one ring per ROI source. We mutate it
    in-place by appending the current crop after the decode attempt.
    """
    tokens = decode_with_preprocess(decode_fn, crop)
    used_stack = False
    if not tokens and len(crop_ring) >= 1:
        # YOLO ROI wobbles a few px between frames; align all prior crops
        # to the current shape before stacking. cv2.resize is ~free
        # compared to the decode work that follows.
        aligned = [_resize_to(c, crop.shape) for c in crop_ring] + [crop]
        stacked = np.median(np.stack(aligned, axis=0), axis=0).astype(np.uint8)
        tokens = decode_with_preprocess(decode_fn, stacked)
        if tokens:
            used_stack = True
    crop_ring.append(crop)
    # decode_with_preprocess falls off the end (returns None) when every
    # variant fails. Normalise to an empty list so the caller's
    # `for token in tokens:` never sees None - latent bug in the old
    # decode_with_preprocess too, only exposed on streams with corrupt
    # HEVC frames where every preprocess variant misses.
    return tokens or [], used_stack
    return []


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


def fetch_onvif_rtsp_urls(ip, username='', password='', xaddr='', timeout=5):
    """Fetch RTSP stream URIs from an ONVIF camera via GetProfiles + GetStreamUri.
    Returns list of dicts with profile and url, or empty list on failure.
    """
    import requests
    import re

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

    # Try each URL until one works
    resp = None
    used_url = None
    for media_url in media_urls:
        try:
            print(f"[INFO] Trying ONVIF GetProfiles at {media_url}")
            resp = requests.post(media_url, data=get_profiles,
                                 headers={'Content-Type': 'application/soap+xml'},
                                 timeout=timeout)
            print(f"[INFO] ONVIF GetProfiles {media_url}: HTTP {resp.status_code}")
            if resp.status_code == 200 and 'token="' in resp.text and 'Profiles' in resp.text:
                used_url = media_url
                break
            # Log response body for debugging
            if resp.status_code != 200 or 'Fault' in resp.text:
                print(f"[WARN] ONVIF response: {resp.text[:500]}")
        except Exception as e:
            print(f"[WARN] ONVIF GetProfiles failed at {media_url}: {e}")

    if not resp or not used_url:
        print(f"[WARN] All ONVIF endpoints failed for {ip}")
        return []

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
        return []

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
                rtsp_url = uri_match.group(1)
                name = profile_names[i] if i < len(profile_names) else token
                res = profile_resolutions.get(token, '')
                label = f"{name} ({res})" if res else name
                results.append({'profile': label, 'url': rtsp_url})
                print(f"[INFO] Camera {ip} profile '{label}': {rtsp_url}")
            else:
                print(f"[WARN] No Uri found in GetStreamUri response for {token}")
        except Exception as e:
            print(f"[WARN] GetStreamUri failed for {ip} profile {token}: {e}")

    return results


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


def _resolve_camera_ip(camera_spec):
    """Re-discover camera IP by UUID via ONVIF. Returns IP or None."""
    cameras = discover_onvif_cameras(timeout=3)
    return resolve_camera_address(camera_spec, cameras)


def _fetch_onvif_session_url(camera_spec, username, password, profile='main'):
    """Fetch a fresh ONVIF session RTSP URL, re-discovering IP by UUID first."""
    ip = _resolve_camera_ip(camera_spec)
    if not ip:
        print(f"[WARN] Could not discover camera for '{camera_spec}'")
        return None
    urls = fetch_onvif_rtsp_urls(ip, username=username, password=password)
    if not urls:
        return None
    profile_lower = profile.lower()
    for u in urls:
        if profile_lower in u['profile'].lower():
            print(f"[INFO] Got ONVIF session URL for '{profile}' ({ip}): {u['url']}")
            return u['url']
    print(f"[INFO] Using first ONVIF session URL ({ip}): {urls[0]['url']}")
    return urls[0]['url']


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

        # Special path: /onvif/main or /onvif/sub → fetch fresh session URL
        # Re-discovers camera IP by UUID on each call (handles IP changes)
        if path.startswith('/onvif/'):
            profile = path.split('/')[-1]  # 'main' or 'sub'
            builder = lambda _spec=camera_spec, _u=user, _p=passwd, _pr=profile: \
                _fetch_onvif_session_url(_spec, _u, _p, _pr)
            session_url = builder()
            if session_url:
                return session_url, builder
            print(f"[WARN] ONVIF session URL fetch failed, falling back to /stream2")
            path = '/stream2'

        url = f"rtsp://{user}:{passwd}@{ip}:{port}{path}"
        return resolve_mdns_in_url(url), None

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
        # Minimize FFmpeg internal buffer to reduce RTSP latency
        os.environ['OPENCV_FFMPEG_CAPTURE_OPTIONS'] = \
            'rtsp_transport;tcp|fflags;nobuffer|flags;low_delay|max_delay;500000'

        while _running and not self._stopped:
            # Refresh URL if we have a builder (e.g. ONVIF session URLs)
            if self._url_builder:
                fresh = self._url_builder()
                if fresh:
                    self.url = fresh
            cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
            if not cap.isOpened():
                print(f"[WARN] [{self.name}] Cannot open stream, retrying...")
                time.sleep(2)
                continue

            self._w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            self._h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            self._fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
            self._max_buffer = int(self._fps * self._buffer_seconds) + 10
            print(f"[INFO] [{self.name}] Stream opened: "
                  f"{self._w}x{self._h} @ {self._fps:.1f} FPS")

            while _running and not self._stopped:
                ret, frame = cap.read()
                if not ret:
                    print(f"[WARN] [{self.name}] Frame read failed, "
                          f"reconnecting...")
                    break
                now = time.time()
                with self._lock:
                    self._frame = frame
                    self._frame_ts = now
                    self._frame_count += 1
                    self._ring_buffer.append((frame, now))
                    if len(self._ring_buffer) > self._max_buffer:
                        self._ring_buffer = \
                            self._ring_buffer[-self._max_buffer:]

            cap.release()
            if _running:
                time.sleep(1)

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

        # Save a JPG thumbnail of the moment-of-detection frame so the
        # access log can render a clickable preview instead of just a
        # bare "Play" button. The last pre_frame is roughly when the QR
        # entered view; falls back to first post_frame, then nothing
        # (older rows already render "Play" for null thumbnail_path).
        try:
            thumb_frame = None
            if pre_frames:
                thumb_frame = pre_frames[-1][0]
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
        for codec, preset in [('h264_nvenc', 'p1'), ('libx264', 'ultrafast')]:
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
                 url_builder=None):
        self.name = name
        self.camera_url = camera_url
        self.door = door_controller
        self.valid_tokens = set(valid_tokens)
        self.url_builder = url_builder  # callable for ONVIF session refresh
        self.camera = None  # set after starting
        self.token_count = 0
        self.door_opens = 0
        self.denied_count = 0
        self._last_processed_frame = 0
        # Per-door rolling buffer of recent QR crops, used by
        # decode_with_stacking to median-out rolling-shutter bands on
        # per-frame decode miss. One ring per door so concurrent doors
        # don't pollute each other's stacks.
        self.crop_ring = deque(maxlen=STACK_N)
        # Latest detection state for the /api/doors/<name>/live MJPEG
        # preview. List of (x1, y1, x2, y2, conf, token_or_None) tuples
        # plus a wall-clock timestamp; the live endpoint draws these as
        # an overlay on the most recent CameraReader frame. Updated
        # under _live_lock from the main detect loop so a separate
        # MJPEG-serving thread can read them without tearing.
        self.latest_detections = []
        self.latest_detections_ts = 0.0
        self._live_lock = threading.Lock()


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

EVENT_COOLDOWN = 10  # seconds between duplicate events per door+token


def run_multi_door(doors, det_type, det_model, decode_fn, conf=0.3, skip=1,
                   video_logger=None):
    """Main loop: process all doors, sharing GPU for YOLO inference."""
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

    print(f"\n[INFO] Processing {len(doors)} door(s) (skip={skip}, conf={conf})")
    print(f"[INFO] Press Ctrl+C to stop\n")

    while _running:
        any_frame = False

        for door_cfg in doors:
            frame, frame_count, capture_ts = door_cfg.camera.get_frame()
            if frame is None:
                continue
            if frame_count == door_cfg._last_processed_frame:
                continue  # already processed this frame
            if frame_count % skip != 0:
                door_cfg._last_processed_frame = frame_count
                continue

            door_cfg._last_processed_frame = frame_count
            frame_lag = time.time() - capture_ts if capture_ts else 0
            any_frame = True
            total_processed += 1
            interval_processed += 1

            # YOLO detection (GPU, shared)
            detections = detect_qr_regions(det_type, det_model, frame,
                                           conf=conf)

            # Per-frame snapshot of detections + their first decoded
            # token. Published to door_cfg.latest_detections so the
            # /api/doors/<name>/live MJPEG endpoint can draw an overlay
            # on the latest CameraReader frame. Updated under the lock
            # so the live thread doesn't see torn writes.
            live_dets = []

            for x1, y1, x2, y2, confidence in detections:
                crop = frame[y1:y2, x1:x2]
                if crop.size == 0:
                    continue

                tokens, _ = decode_with_stacking(
                    decode_fn, crop, door_cfg.crop_ring)
                live_dets.append(
                    (int(x1), int(y1), int(x2), int(y2),
                     float(confidence),
                     tokens[0] if tokens else None))

                for token in tokens:
                    # Cooldown: skip if same door+token seen recently
                    now_t = time.time()
                    event_key = (door_cfg.name, token)
                    if now_t - last_event.get(event_key, 0) < EVENT_COOLDOWN:
                        continue
                    last_event[event_key] = now_t

                    door_cfg.token_count += 1
                    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

                    # Check token against DB (live) with fallback to config
                    try:
                        from web.db import get_all_active_tokens
                        is_valid = token in get_all_active_tokens()
                    except Exception:
                        is_valid = token in door_cfg.valid_tokens

                    if is_valid:
                        event = "ACCESS-GRANTED"
                        print(f"[{ts}] [{door_cfg.name}] ACCESS GRANTED: "
                              f"{token} (frame={frame_count}, "
                              f"conf={confidence:.2f}, "
                              f"lag={frame_lag:.1f}s)")
                        if door_cfg.door:
                            if door_cfg.door.open(token):
                                door_cfg.door_opens += 1
                        else:
                            print(f"[{ts}] [{door_cfg.name}] DRY RUN: "
                                  f"would open door")
                    else:
                        event = "ACCESS-DENIED"
                        door_cfg.denied_count += 1
                        print(f"[{ts}] [{door_cfg.name}] ACCESS DENIED: "
                              f"{token} (frame={frame_count}, "
                              f"conf={confidence:.2f}, "
                              f"lag={frame_lag:.1f}s)")

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
                                            thumbnail_path=thumb_rel)
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
    parser.add_argument('--skip', type=int, default=3,
                        help='Process every Nth frame (default: 3)')
    parser.add_argument('--model-size', default='s',
                        choices=['n', 's', 'm', 'l'],
                        help='YOLO model size (default: s)')
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
        else:
            shelly_spec = door_def.get('shelly', 'auto')
            shelly_ip = resolve_shelly_address(shelly_spec, discovered_shelly)
            if shelly_ip:
                door_ctrl = DoorController(name, shelly_ip, open_seconds,
                                           password=shelly_pass)
                status = door_ctrl.status()
                print(f"[INFO] [{name}] Shelly {shelly_ip} "
                      f"(auth={'yes' if shelly_pass else 'no'}): {status}")
            else:
                print(f"[WARN] [{name}] No Shelly found, door control disabled")
                door_ctrl = None

        print(f"[INFO] [{name}] Camera: {camera_url}")
        print(f"[INFO] [{name}] Valid tokens: {len(valid_tokens)}")

        doors.append(DoorConfig(name, camera_url, door_ctrl, valid_tokens,
                               url_builder=url_builder))

    # Shared GPU detector
    det_type, det_model = create_detector(model_size=args.model_size)
    decode_fn = create_decoder()

    video_logger = VideoLogger(args.video_log_dir)
    print(f"[INFO] Video logs: {args.video_log_dir}")

    run_multi_door(doors, det_type, det_model, decode_fn,
                   conf=args.conf, skip=args.skip,
                   video_logger=video_logger)


if __name__ == "__main__":
    main()
