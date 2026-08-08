"""Unit tests for pipeline backend selection + fallback.

Covers the pure logic (auto-detect, decode pairing) and that
build_detector degrades to cpu on any hardware/export failure. The
heavy detector classes (qrdet/ultralytics) are monkeypatched - no GPU,
model, or camera needed.
"""

import sys
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pipeline as pl


class TestOpenVinoGpuDevice(unittest.TestCase):
    def result_for(self, devices):
        module = SimpleNamespace(
            Core=lambda: SimpleNamespace(available_devices=devices))
        with mock.patch.dict(sys.modules, {'openvino': module}):
            return pl._openvino_gpu_device()

    def test_generic_gpu(self):
        self.assertEqual(self.result_for(['CPU', 'GPU']), 'GPU')

    def test_indexed_gpu(self):
        self.assertEqual(self.result_for(['CPU', 'GPU.0', 'GPU.1']),
                         'GPU.0')

    def test_cpu_only(self):
        self.assertIsNone(self.result_for(['CPU']))

    def test_core_failure(self):
        def fail():
            raise RuntimeError('plugin failure')

        with mock.patch.dict(sys.modules,
                             {'openvino': SimpleNamespace(Core=fail)}):
            self.assertIsNone(pl._openvino_gpu_device())


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
        detector = _StubDetector()
        with mock.patch.object(pl, '_openvino_gpu_device',
                               return_value='GPU.0'), \
             mock.patch.object(pl, 'ensure_export', return_value='ov'), \
             mock.patch.object(pl, 'UltralyticsDetector',
                               return_value=detector) as constructor:
            det, backend = pl.build_detector('igpu', 'n')
            self.assertEqual(backend, 'igpu')
            self.assertIs(det, detector)
            constructor.assert_called_once_with(
                'ov', 'intel:gpu.0', 'OpenVINO', 'Intel iGPU')

    def test_igpu_unavailable_falls_back_before_export(self):
        with mock.patch.object(pl, '_openvino_gpu_device',
                               return_value=None), \
             mock.patch.object(pl, 'ensure_export') as export, \
             mock.patch.object(pl, 'QrdetDetector', _StubDetector):
            det, backend = pl.build_detector('igpu', 'n')
            self.assertEqual(backend, 'cpu')
            self.assertIsInstance(det, _StubDetector)
            export.assert_not_called()

    def test_igpu_export_failure_falls_back_to_cpu(self):
        with mock.patch.object(pl, '_openvino_gpu_device',
                               return_value='GPU'), \
             mock.patch.object(pl, 'ensure_export',
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
