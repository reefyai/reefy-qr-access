"""Unit tests for video_decode backend selection.

Covers the auto-detection priority (nvdec -> vaapi -> cpu), forced
overrides, the ffprobe geometry parser, and label mapping. No real
GPU / camera needed - the hardware probes are monkeypatched.
"""

import sys
import json
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import video_decode as vd


class TestDetectBackend(unittest.TestCase):
    def test_forced_value_returned_as_is(self):
        for forced in ('cpu', 'vaapi', 'nvdec'):
            self.assertEqual(vd.detect_backend(forced), forced)

    def test_auto_prefers_nvdec_when_nvidia_present(self):
        with mock.patch.object(vd, '_nvdec_available', return_value=True), \
             mock.patch.object(vd, '_vaapi_present', return_value=True):
            self.assertEqual(vd.detect_backend('auto'), 'nvdec')

    def test_auto_uses_vaapi_when_render_node_present_no_nvidia(self):
        with mock.patch.object(vd, '_nvdec_available', return_value=False), \
             mock.patch.object(vd, '_vaapi_present', return_value=True):
            self.assertEqual(vd.detect_backend('auto'), 'vaapi')

    def test_auto_falls_back_to_cpu_when_no_hardware(self):
        with mock.patch.object(vd, '_nvdec_available', return_value=False), \
             mock.patch.object(vd, '_vaapi_present', return_value=False):
            self.assertEqual(vd.detect_backend('auto'), 'cpu')

    def test_vaapi_present_checks_render_node(self):
        with mock.patch('os.path.exists', return_value=True) as ex:
            self.assertTrue(vd._vaapi_present())
            ex.assert_called_with(vd.RENDER_NODE)
        with mock.patch('os.path.exists', return_value=False):
            self.assertFalse(vd._vaapi_present())


class TestProbeStream(unittest.TestCase):
    def _run(self, stdout, returncode=0, stderr=''):
        cp = mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)
        with mock.patch('subprocess.run', return_value=cp):
            return vd.probe_stream('rtsp://x')

    def test_parses_width_height_fps(self):
        out = json.dumps({'streams': [
            {'width': 1920, 'height': 1080, 'r_frame_rate': '30/1'}]})
        self.assertEqual(self._run(out), (1920, 1080, 30.0))

    def test_fractional_frame_rate(self):
        out = json.dumps({'streams': [
            {'width': 640, 'height': 360, 'r_frame_rate': '30000/1001'}]})
        w, h, fps = self._run(out)
        self.assertEqual((w, h), (640, 360))
        self.assertAlmostEqual(fps, 29.97, places=2)

    def test_ffprobe_failure_returns_zeros(self):
        self.assertEqual(self._run('', returncode=1), (0, 0, 0.0))

    def test_auth_failure_logs_actionable_message_without_url(self):
        stderr = ('method DESCRIBE failed: 401 Unauthorized\n'
                  'rtsp://admin:secret@camera.example/stream2')
        with mock.patch('builtins.print') as printer:
            result = self._run('', returncode=1, stderr=stderr)
        self.assertEqual(result, (0, 0, 0.0))
        message = printer.call_args.args[0]
        self.assertIn('rejected RTSP authentication', message)
        self.assertIn('verify the camera username and password', message)
        self.assertNotIn('secret', message)
        self.assertNotIn('rtsp://', message)

    def test_connection_refused_logs_address_and_port_hint(self):
        with mock.patch('builtins.print') as printer:
            result = self._run('', returncode=1,
                               stderr='Connection refused')
        self.assertEqual(result, (0, 0, 0.0))
        self.assertIn('verify the camera address and port',
                      printer.call_args.args[0])

    def test_garbage_output_returns_zeros(self):
        self.assertEqual(self._run('not json'), (0, 0, 0.0))


class TestAuthenticationStop(unittest.TestCase):
    def test_hardware_probe_raises_on_rejected_credentials(self):
        reader = vd.FFmpegReader('rtsp://x', 'vaapi', 'test camera')
        with mock.patch.object(
                vd, 'probe_stream_diagnostic',
                return_value=(0, 0, 0.0, 'auth')):
            with self.assertRaises(vd.StreamAuthenticationError):
                reader.open()

    def test_cpu_failure_diagnoses_auth_and_raises(self):
        fake_reader = mock.Mock()
        fake_reader.open.return_value = False
        with mock.patch.object(vd, 'Cv2Reader', return_value=fake_reader), \
             mock.patch.object(
                vd, 'probe_stream_diagnostic',
                return_value=(0, 0, 0.0, 'auth')):
            with self.assertRaises(vd.StreamAuthenticationError):
                vd.open_reader('rtsp://x', 'cpu', 'test camera')


class TestBackendLabel(unittest.TestCase):
    def test_known_labels(self):
        self.assertIn('NVDEC', vd.backend_label('nvdec'))
        self.assertIn('VAAPI', vd.backend_label('vaapi'))
        self.assertIn('software', vd.backend_label('cpu'))

    def test_unknown_label_passthrough(self):
        self.assertEqual(vd.backend_label('weird'), 'weird')


if __name__ == '__main__':
    unittest.main()
