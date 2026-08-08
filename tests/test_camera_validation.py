"""Unit tests for pre-save door camera validation."""

from urllib.parse import unquote, urlparse

from web.services.camera_validation import validate_camera


CAMERA = {
    'ip': '192.0.2.10',
    'xaddr': 'http://192.0.2.10:2020/onvif/device_service',
}
STREAMS = [
    {'profile': 'mainStream', 'url': 'rtsp://192.0.2.10:554/stream1'},
    {'profile': 'minorStream', 'url': 'rtsp://192.0.2.10:554/stream2'},
]


def _fetch(urls, failure=None):
    def fake(ip, username='', password='', xaddr='', diagnostics=False):
        assert diagnostics is True
        return urls, failure
    return fake


def test_rejected_onvif_credentials_return_422_without_rtsp_probe():
    called = False

    def probe(*args, **kwargs):
        nonlocal called
        called = True

    result = validate_camera(
        CAMERA, 'operator', 'wrong-password', '/onvif/main', 554,
        fetch_rtsp=_fetch([], 'auth'), probe_stream=probe)

    assert result['ok'] is False
    assert result['status'] == 422
    assert 'username or password' in result['error']
    assert called is False


def test_unreachable_onvif_endpoint_returns_503():
    result = validate_camera(
        CAMERA, 'operator', 'password', '/onvif/main', 554,
        fetch_rtsp=_fetch([], 'unreachable'),
        probe_stream=lambda *args, **kwargs: None)

    assert result['ok'] is False
    assert result['status'] == 503
    assert 'unreachable' in result['error']


def test_rejected_rtsp_credentials_return_422():
    result = validate_camera(
        CAMERA, 'operator', 'wrong-password', '/onvif/main', 554,
        fetch_rtsp=_fetch(STREAMS),
        probe_stream=lambda *args, **kwargs: (0, 0, 0.0, 'auth'))

    assert result['ok'] is False
    assert result['status'] == 422
    assert 'RTSP video' in result['error']


def test_success_probes_selected_profile_with_encoded_credentials():
    captured = {}

    def probe(url, **kwargs):
        captured['url'] = url
        return 1920, 1080, 30.0, None

    result = validate_camera(
        CAMERA, 'door user', 'p@ss:word', '/onvif/main', 554,
        fetch_rtsp=_fetch(STREAMS), probe_stream=probe)

    assert result == {'ok': True, 'status': 200, 'error': None}
    parsed = urlparse(captured['url'])
    assert unquote(parsed.username) == 'door user'
    assert unquote(parsed.password) == 'p@ss:word'
    assert parsed.path == '/stream1'


def test_sub_profile_accepts_minor_camera_naming():
    captured = {}

    def probe(url, **kwargs):
        captured['path'] = urlparse(url).path
        return 1280, 720, 15.0, None

    result = validate_camera(
        CAMERA, 'operator', 'password', '/onvif/sub', 554,
        fetch_rtsp=_fetch(STREAMS), probe_stream=probe)

    assert result['ok'] is True
    assert captured['path'] == '/stream2'
