"""Unit tests for OnvifRelayController (qr_live): the no-Shelly door
opener that drives a camera's ONVIF alarm relay.

These mock the HTTP layer (no camera needed) and exercise the exact
paths that run on a real grant: relay-token auto-discovery, the
Monostable/DelayTime setup, and the active-state trigger. The
auto-discovery test is what would have caught the `re` NameError that
shipped in v2026.06.14-00 (re.search in _ensure_configured with re not
in scope) - it reproduces in milliseconds without a camera.

Runs under both `python3 -m unittest` and pytest. Requires qr_live's
imports (cv2/numpy/requests), so run inside the app image or an env
with the detector deps.
"""

import unittest
from unittest import mock

import qr_live


_RELAY_OUTPUTS = (
    '<tds:GetRelayOutputsResponse>'
    '<tds:RelayOutputs token="AlarmOut_0">'
    '<tt:Properties><tt:Mode>Monostable</tt:Mode>'
    '<tt:DelayTime>PT5S</tt:DelayTime><tt:IdleState>closed</tt:IdleState>'
    '</tt:Properties></tds:RelayOutputs>'
    '</tds:GetRelayOutputsResponse>'
)


class _Resp:
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status


class _FakeSession:
    """Records SOAP bodies; canned responses keyed by operation."""

    def __init__(self):
        self.posts = []
        self.urls = []
        self.fault_on = None  # substring -> return a SOAP fault

    def post(self, url, data=None, headers=None, timeout=None):
        body = data.decode() if isinstance(data, (bytes, bytearray)) else (data or '')
        self.urls.append(url)
        self.posts.append(body)
        if self.fault_on and self.fault_on in body:
            return _Resp('<soap:Fault><soap:Reason>nope</soap:Reason></soap:Fault>', 500)
        if 'GetRelayOutputs' in body:
            return _Resp(_RELAY_OUTPUTS)
        return _Resp('<ok/>')


class OnvifRelayControllerTests(unittest.TestCase):
    def setUp(self):
        self._orig = qr_live.requests.Session
        self.fake = _FakeSession()
        qr_live.requests.Session = lambda: self.fake

    def tearDown(self):
        qr_live.requests.Session = self._orig

    def test_autodiscovers_token_and_sets_monostable(self):
        # relay_token='auto' -> __init__ calls _ensure_configured, which
        # parses GetRelayOutputs with re.search. This is the path that
        # NameError'd in v2026.06.14-00.
        c = qr_live.OnvifRelayController('d', '1.2.3.4', 'admin', 'pw',
                                         open_seconds=5, relay_token='auto')
        self.assertEqual(c.relay_token, 'AlarmOut_0',
                         f'auto-discovery did not resolve the relay token '
                         f'(posts={self.fake.posts})')
        self.assertTrue(
            any('SetRelayOutputSettings' in p and 'Monostable' in p
                and 'PT5S' in p for p in self.fake.posts),
            f'startup did not set Monostable/PT5S: {self.fake.posts}')

    def test_open_fires_active_state(self):
        c = qr_live.OnvifRelayController('d', '1.2.3.4', 'admin', 'pw',
                                         open_seconds=5, relay_token='auto')
        self.fake.posts.clear()
        self.assertTrue(c.open('tok12345abcdef'), 'open() should return True')
        self.assertTrue(
            any('SetRelayOutputState' in p and 'AlarmOut_0' in p
                and 'active' in p for p in self.fake.posts),
            f'open() did not fire SetRelayOutputState active: {self.fake.posts}')

    def test_uses_discovered_onvif_device_url(self):
        device_url = 'http://192.0.2.10:2020/onvif/device_service'
        qr_live.OnvifRelayController(
            'd', '192.0.2.10', 'admin', 'pw', open_seconds=5,
            relay_token='auto', device_url=device_url)
        self.assertTrue(self.fake.urls)
        self.assertTrue(all(url == device_url for url in self.fake.urls))

    def test_open_returns_false_on_fault(self):
        c = qr_live.OnvifRelayController('d', '1.2.3.4', 'admin', 'pw',
                                         open_seconds=5, relay_token='AlarmOut_0')
        self.fake.fault_on = 'SetRelayOutputState'
        self.assertFalse(c.open('tok12345abcdef'),
                         'open() must return False on a SOAP fault')

    def test_regrant_cooldown_equals_open_seconds(self):
        # Cooldown is the beep length (open_seconds), not open_seconds+2,
        # so a repeat scan re-fires as soon as the open finishes.
        c = qr_live.OnvifRelayController('d', '1.2.3.4', 'admin', 'pw',
                                         open_seconds=5, relay_token='AlarmOut_0')
        self.assertEqual(c._cooldown, 5)
        self.assertTrue(c.open('tok'), 'first open should fire')
        self.assertFalse(c.open('tok'), 'within cooldown -> blocked')
        c._last_open -= 6  # pretend the open window (5s) has elapsed
        self.assertTrue(c.open('tok'), 'after cooldown -> fires again')


class OnvifStreamDiscoveryTests(unittest.TestCase):
    def test_rtsp_discovery_reports_auth_failure(self):
        device_url = 'http://192.0.2.10:2020/onvif/device_service'
        with mock.patch.object(
                qr_live, '_discover_media_service',
                return_value=[device_url]), \
             mock.patch.object(
                qr_live.requests, 'post', return_value=_Resp('', 401)) as post:
            urls, failure = qr_live.fetch_onvif_rtsp_urls(
                '192.0.2.10', username='operator', password='wrong-password',
                xaddr=device_url, diagnostics=True)

        self.assertEqual(urls, [])
        self.assertEqual(failure, 'auth')
        self.assertEqual(post.call_count, 1)

    def test_session_fetch_preserves_discovered_xaddr(self):
        camera = {
            'ip': '192.0.2.10',
            'uuid': '00000000-0000-4000-8000-000000000001',
            'name': 'Synthetic Camera',
            'hardware': 'Test Model',
            'xaddr': 'http://192.0.2.10:2020/onvif/device_service',
        }
        streams = [{
            'profile': 'mainStream',
            'url': 'rtsp://192.0.2.10:554/stream1',
        }]
        spec = f"onvif:uuid:{camera['uuid']}"
        with mock.patch.object(
                qr_live, 'discover_onvif_cameras', return_value=[camera]), \
             mock.patch.object(
                qr_live, 'fetch_onvif_rtsp_urls',
                return_value=(streams, None)) as fetch:
            url = qr_live._fetch_onvif_session_url(
                spec, 'operator', 'password', 'main')

        self.assertIn('/stream1', url)
        self.assertEqual(fetch.call_args.kwargs['xaddr'], camera['xaddr'])

    def test_session_fetch_maps_sub_profile_to_minor_stream(self):
        camera = {
            'ip': '192.0.2.10',
            'uuid': '00000000-0000-4000-8000-000000000001',
            'name': 'Synthetic Camera',
            'hardware': 'Test Model',
            'xaddr': 'http://192.0.2.10:2020/onvif/device_service',
        }
        streams = [
            {'profile': 'mainStream',
             'url': 'rtsp://192.0.2.10:554/stream1'},
            {'profile': 'minorStream',
             'url': 'rtsp://192.0.2.10:554/stream2'},
        ]
        spec = f"onvif:uuid:{camera['uuid']}"
        with mock.patch.object(
                qr_live, 'discover_onvif_cameras', return_value=[camera]), \
             mock.patch.object(
                qr_live, 'fetch_onvif_rtsp_urls',
                return_value=(streams, None)):
            url = qr_live._fetch_onvif_session_url(
                spec, 'operator', 'password', 'sub')

        self.assertIn('/stream2', url)

    def test_build_pauses_rejected_credentials_without_reprobing(self):
        camera = {
            'ip': '192.0.2.10',
            'uuid': '00000000-0000-4000-8000-000000000001',
            'name': 'Synthetic Camera',
            'hardware': 'Test Model',
            'xaddr': 'http://192.0.2.10:2020/onvif/device_service',
        }
        spec = f"onvif:uuid:{camera['uuid']}"
        door = {
            'camera_user': 'operator',
            'camera_pass': 'wrong-password',
            'camera_path': '/onvif/main',
            'camera_port': 554,
        }
        with mock.patch.object(
                qr_live, '_fetch_onvif_session_url',
                side_effect=qr_live.CameraAuthenticationError) as fetch:
            _url, builder = qr_live.build_rtsp_url(spec, door, [camera])
            self.assertEqual(fetch.call_count, 1)
            with self.assertRaises(qr_live.CameraAuthenticationError):
                builder()
            self.assertEqual(fetch.call_count, 1)

    def test_relay_health_check_preserves_discovered_xaddr(self):
        device_url = 'http://192.0.2.10:2020/onvif/device_service'
        with mock.patch.object(
                qr_live.requests, 'post',
                return_value=_Resp(_RELAY_OUTPUTS)) as post:
            result = qr_live.onvif_relay_check(
                '192.0.2.10', 'operator', 'password',
                device_url=device_url)

        self.assertTrue(result['ok'])
        self.assertEqual(post.call_args.args[0], device_url)


if __name__ == '__main__':
    unittest.main(verbosity=2)
