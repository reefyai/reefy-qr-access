"""Unit tests for the real-hardware performance gate's fallback checks."""

import importlib.util
from pathlib import Path
import unittest


_PATH = Path(__file__).resolve().parents[1] / 'tools' / 'run_perf_regression.py'
_SPEC = importlib.util.spec_from_file_location('run_perf_regression', _PATH)
perf = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(perf)


class TestBackendGate(unittest.TestCase):
    base = {'detect_ms': 10, 'cpu_cores': 1}
    tolerance = {'detect_ms_pct': 20, 'cpu_cores_pct': 20}

    def result(self, backend, pipeline, decode):
        return {
            'ok': True,
            'backend': backend,
            'pipeline': pipeline,
            'decode': decode,
            'detect_ms': 10,
            'cpu_cores': 1,
        }

    def test_igpu_requires_openvino_and_vaapi(self):
        ok, _ = perf.check(
            self.result('igpu', 'igpu', 'vaapi'), self.base, self.tolerance)
        self.assertTrue(ok)

    def test_igpu_rejects_silent_cpu_fallback(self):
        ok, reason = perf.check(
            self.result('igpu', 'cpu', 'cpu (hw failed)'),
            self.base,
            self.tolerance,
        )
        self.assertFalse(ok)
        self.assertIn('backend fallback', reason)

    def test_gpu_requires_cuda_and_nvdec(self):
        ok, _ = perf.check(
            self.result('gpu', 'gpu', 'nvdec'), self.base, self.tolerance)
        self.assertTrue(ok)


if __name__ == '__main__':
    unittest.main()
