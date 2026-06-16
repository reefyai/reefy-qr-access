#!/usr/bin/env python3
"""Cross-platform pipeline perf-regression gate.

Runs the full-pipeline benchmark (tests/e2e/pipeline_bench.py, baked into
the qr-access image) on the real hardware boxes via ssh+docker, compares
each config against the committed baselines (tests/e2e/baselines/<box>.json),
prints the result table, and exits non-zero on any regression. This cannot
run in GitHub CI - it needs real GPUs and synthetic camera streams - so it
is a manual/pre-release gate.

Hard gates per config: detect_ms and cpu_cores must stay within the
baseline tolerance, and the synthetic QR token must decode (ok=true). fps
is reported but not gated (it is test-capped/noisy).

Usage:
    python3 tools/run_perf_regression.py [--seconds 12] [--update-baselines]

Edit BOXES below for ssh aliases / image tag / GPU docker args.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BASELINE_DIR = REPO / 'tests' / 'e2e' / 'baselines'
IMAGE = 'ghcr.io/reefyai/reefy-qr-access:v2026.06.16-00'

# Per hardware class: the docker args granting the accelerator, the
# (backend, env-prefix) configs to measure, and the baseline file.
# CUDA_VISIBLE_DEVICES='' forces a true CPU run on the GPU box.
# SSH targets are NOT hardcoded here (they contain device access URLs) -
# supply them via tools/perf-boxes.json (gitignored; see
# tools/perf-boxes.example.json) or the env var named in 'ssh_env'.
BOX_DEFS = {
    'intel-igpu': {
        'gpu_args': '--device /dev/dri:/dev/dri',
        'configs': [('cpu', ''), ('igpu', '')],
        'baseline': 'intel-igpu.json',
        'ssh_env': 'PERF_SSH_INTEL_IGPU',
    },
    'nvidia-gpu': {
        'gpu_args': '--device nvidia.com/gpu=all',
        'configs': [('cpu', 'CUDA_VISIBLE_DEVICES='), ('gpu', '')],
        'baseline': 'nvidia-gpu.json',
        'ssh_env': 'PERF_SSH_NVIDIA_GPU',
    },
}
RESOLUTIONS = ('360p', '720p')


def load_ssh_targets():
    """Resolve ssh targets from tools/perf-boxes.json (gitignored) or the
    per-box env var, keeping device access URLs out of committed code."""
    import os
    targets = {}
    cfg = Path(os.environ.get('PERF_BOXES_FILE',
                              REPO / 'tools' / 'perf-boxes.json'))
    if cfg.exists():
        targets.update(json.loads(cfg.read_text()))
    for box, d in BOX_DEFS.items():
        if box not in targets and os.environ.get(d['ssh_env']):
            targets[box] = os.environ[d['ssh_env']]
    return targets


def run_bench(box, ssh_target, backend, env_prefix, res, seconds):
    """ssh+docker-run one bench config; return its RESULT_JSON dict."""
    cfg = BOX_DEFS[box]
    inner = (f"{env_prefix} python3 tests/e2e/pipeline_bench.py "
             f"--backend {backend} --res {res} --seconds {seconds}")
    docker = (f"sudo docker run --rm --network host {cfg['gpu_args']} "
              f"-e MODEL_CACHE=/models {IMAGE} bash -lc '{inner}'")
    out = subprocess.run(
        ['ssh', '-o', 'ConnectTimeout=15', ssh_target, docker],
        capture_output=True, text=True, timeout=seconds + 240)
    m = re.search(r'RESULT_JSON (\{.*\})', out.stdout + out.stderr)
    if not m:
        return {'backend': backend, 'res': res, 'ok': False,
                'error': (out.stderr or out.stdout)[-300:]}
    return json.loads(m.group(1))


def check(measured, base, tol):
    """True if measured is within tolerance of the baseline + decoded."""
    if not measured.get('ok'):
        return False, 'token not decoded'
    dmax = base['detect_ms'] * (1 + tol['detect_ms_pct'] / 100)
    cmax = base['cpu_cores'] * (1 + tol['cpu_cores_pct'] / 100)
    if measured['detect_ms'] > dmax:
        return False, f"detect_ms {measured['detect_ms']} > {dmax:.0f}"
    if measured['cpu_cores'] > cmax:
        return False, f"cpu_cores {measured['cpu_cores']} > {cmax:.2f}"
    return True, 'ok'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--seconds', type=int, default=12)
    ap.add_argument('--update-baselines', action='store_true')
    args = ap.parse_args()

    ssh_targets = load_ssh_targets()
    rows, failures, new_baselines = [], [], {}
    for box, cfg in BOX_DEFS.items():
        ssh_target = ssh_targets.get(box)
        if not ssh_target:
            print(f"[skip] {box}: no ssh target (set {cfg['ssh_env']} or add "
                  f"to tools/perf-boxes.json)")
            continue
        base = json.loads((BASELINE_DIR / cfg['baseline']).read_text())
        tol = base['tolerance']
        new_baselines[box] = dict(base)
        new_baselines[box]['configs'] = dict(base['configs'])
        for backend, env in cfg['configs']:
            for res in RESOLUTIONS:
                key = f'{backend}@{res}'
                print(f"[run] {box} {key} ...", flush=True)
                r = run_bench(box, ssh_target, backend, env, res, args.seconds)
                if args.update_baselines and r.get('ok'):
                    new_baselines[box]['configs'][key] = {
                        'detect_ms': r['detect_ms'],
                        'cpu_cores': r['cpu_cores'], 'fps': r['fps']}
                ok, why = (True, 'baseline') if args.update_baselines else \
                    check(r, base['configs'].get(key, {}), tol)
                rows.append((box, key, r, ok, why))
                if not ok:
                    failures.append(f"{box} {key}: {why}")

    if args.update_baselines:
        for box, data in new_baselines.items():
            (BASELINE_DIR / BOX_DEFS[box]['baseline']).write_text(
                json.dumps(data, indent=2) + '\n')
        print(f"\nUpdated baselines for {', '.join(new_baselines)}.")
        return

    print(f"\n{'box':<11}{'config':<11}{'fps':>7}{'cpu':>7}"
          f"{'detect':>9}{'qr':>7}  result")
    for box, key, r, ok, why in rows:
        print(f"{box:<11}{key:<11}{r.get('fps', 0):>7}"
              f"{r.get('cpu_cores', 0):>7}{r.get('detect_ms', 0):>9}"
              f"{r.get('qrdecode_ms', 0):>7}  "
              f"{'PASS' if ok else 'FAIL: ' + why}")

    if failures:
        print(f"\n{len(failures)} regression(s):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nAll configs within baseline tolerance.")


if __name__ == '__main__':
    main()
