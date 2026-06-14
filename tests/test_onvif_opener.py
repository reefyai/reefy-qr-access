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
        self.fault_on = None  # substring -> return a SOAP fault

    def post(self, url, data=None, headers=None, timeout=None):
        body = data.decode() if isinstance(data, (bytes, bytearray)) else (data or '')
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

    def test_open_returns_false_on_fault(self):
        c = qr_live.OnvifRelayController('d', '1.2.3.4', 'admin', 'pw',
                                         open_seconds=5, relay_token='AlarmOut_0')
        self.fake.fault_on = 'SetRelayOutputState'
        self.assertFalse(c.open('tok12345abcdef'),
                         'open() must return False on a SOAP fault')


if __name__ == '__main__':
    unittest.main(verbosity=2)
