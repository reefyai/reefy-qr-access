"""Network discovery for ONVIF cameras and Shelly devices."""

import sys
import os

# Add parent dir so we can import from qr_live
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qr_live import discover_onvif_cameras, discover_shelly, fetch_onvif_rtsp_urls


def scan_cameras(timeout=3):
    """Discover ONVIF cameras. Returns list of dicts."""
    return discover_onvif_cameras(timeout=timeout)


def scan_shellys(timeout=5):
    """Discover Shelly devices. Returns dict: device_id -> ip."""
    return discover_shelly(timeout=timeout)


def fetch_camera_rtsp(ip, username='', password='', xaddr='',
                      diagnostics=False):
    """Fetch RTSP URLs from a specific camera using ONVIF."""
    return fetch_onvif_rtsp_urls(ip, username=username, password=password,
                                 xaddr=xaddr, diagnostics=diagnostics)
