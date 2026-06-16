"""Unit tests for pipeline backend selection + fallback.

Covers the pure logic (auto-detect, decode pairing) and that
build_detector degrades to cpu on any hardware/export failure. The
heavy detector classes (qrdet/ultralytics) are monkeypatched - no GPU,
model, or camera needed.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pipeline as pl


class TestDetectPipelineBackend(unittest.TestCase):
    def test_forced_value_returned_as_is(self):
        for forced in ('cpu', 'igpu', 'gpu'):
            self.assertEqual(pl.detect_pipeline_backend(forced), forced)

    def test_auto_prefers_gpu_when_cuda(self):
        with mock.patch.object(pl, '_has_cuda', return_value=True), \
             mock.patch('os.path.exists', return_value=True):
            self.assertEqual(pl.detect_pipeline_backend('auto'), 'gpu')

    def test_auto_uses_igpu_when_render_node_no_cuda(self):
        with mock.patch.object(pl, '_has_cuda', return_value=False), \
             mock.patch('os.path.exists', return_value=True):
            self.assertEqual(pl.detect_pipeline_backend('auto'), 'igpu')

    def test_auto_falls_back_to_cpu(self):
        with mock.patch.object(pl, '_has_cuda', return_value=False), \
             mock.patch('os.path.exists', return_value=False):
            self.assertEqual(pl.detect_pipeline_backend('auto'), 'cpu')


class TestDecodeBackendPairing(unittest.TestCase):
    def test_pairing(self):
        self.assertEqual(pl.decode_backend_for('cpu'), 'cpu')
        self.assertEqual(pl.decode_backend_for('igpu'), 'vaapi')
        self.assertEqual(pl.decode_backend_for('gpu'), 'nvdec')

    def test_unknown_defaults_cpu(self):
        self.assertEqual(pl.decode_backend_for('weird'), 'cpu')


class _StubDetector:
    def __init__(self, *a, **k):
        pass

    def info(self):
        return {'detect': 'stub', 'detect_device': 'stub'}


class TestBuildDetectorFallback(unittest.TestCase):
    def test_igpu_success(self):
        with mock.patch.object(pl, 'ensure_export', return_value='ov'), \
             mock.patch.object(pl, 'UltralyticsDetector', _StubDetector):
            det, backend = pl.build_detector('igpu', 'n')
            self.assertEqual(backend, 'igpu')
            self.assertIsInstance(det, _StubDetector)

    def test_igpu_export_failure_falls_back_to_cpu(self):
        with mock.patch.object(pl, 'ensure_export',
                               side_effect=RuntimeError('no openvino')), \
             mock.patch.object(pl, 'QrdetDetector', _StubDetector):
            det, backend = pl.build_detector('igpu', 'n')
            self.assertEqual(backend, 'cpu')

    def test_gpu_device_failure_falls_back_to_cpu(self):
        # ensure_export ok, but constructing the detector (device init) fails
        with mock.patch.object(pl, 'ensure_export', return_value='eng'), \
             mock.patch.object(pl, 'UltralyticsDetector',
                               side_effect=RuntimeError('no cuda')), \
             mock.patch.object(pl, 'QrdetDetector', _StubDetector):
            det, backend = pl.build_detector('gpu', 's')
            self.assertEqual(backend, 'cpu')

    def test_cpu_backend_uses_qrdet(self):
        with mock.patch.object(pl, 'QrdetDetector', _StubDetector):
            det, backend = pl.build_detector('cpu', 'n')
            self.assertEqual(backend, 'cpu')
            self.assertIsInstance(det, _StubDetector)


if __name__ == '__main__':
    unittest.main()
