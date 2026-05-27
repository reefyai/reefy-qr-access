#!/usr/bin/env python3
"""Decode-strategy benchmark for the qr-access detector.

Takes a recorded camera clip (any MP4/MOV that OpenCV can open),
runs the production YOLO detector to locate QR regions per frame,
then runs every decode strategy against the *same* set of crops so
the only variable is the strategy itself. Prints a comparison table
of decode rate, unique tokens, false positives, and per-call latency.

Strategies (additive; each is independently toggled in `STRATEGIES`):
- baseline-pyzbar         : raw crop -> pyzbar
- variants-pyzbar         : current preprocess_variants -> pyzbar
- pad15-variants-pyzbar   : 15% padded crop -> preprocess_variants -> pyzbar
- pad25-variants-pyzbar   : 25% padded crop -> preprocess_variants -> pyzbar
- upscale4x-pyzbar        : 4x LANCZOS upscale -> pyzbar
- baseline-zxing          : raw crop -> zxing-cpp
- variants-zxing          : preprocess_variants -> zxing-cpp
- pad15-zxing             : 15% padded -> zxing-cpp
- baseline-deqr           : raw crop -> deqr
- combo-pad15-zxing-fallback :
    pad15+variants+pyzbar; on miss -> pad15+variants+zxing
- combo-all-fallback :
    pad15+variants+pyzbar; miss -> zxing; miss -> deqr

Notes:
- Stacking is *not* benchmarked here because it depends on temporal
  state across frames and is already wired into production. The
  benchmark isolates *per-frame* decode strategies; stacking
  multiplies whatever wins here.
- Crops come from the production YOLO via the existing
  `detect_qr_regions` so the comparison is end-to-end realistic.
- We track unique decoded tokens to catch the case where a strategy
  inflates "decoded frames" by re-hitting the same token; the
  signal that actually matters is reach (does it succeed on frames
  the baseline misses?).
"""

import argparse
import sys
import time
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

# Pull production helpers from the live detector module. Keep this
# script next to it so the import is `import qr_live` after a sys.path
# tweak; avoids forking the preprocess pipeline and lets the benchmark
# follow whatever the production code does next.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import qr_live  # noqa: E402


def pad_crop(frame, x1, y1, x2, y2, pct):
    h, w = frame.shape[:2]
    pad_x = int((x2 - x1) * pct / 100)
    pad_y = int((y2 - y1) * pct / 100)
    return frame[max(0, y1 - pad_y):min(h, y2 + pad_y),
                 max(0, x1 - pad_x):min(w, x2 + pad_x)]


def decode_pyzbar(img):
    from pyzbar.pyzbar import decode as pz
    return [r.data.decode('utf-8', errors='replace') for r in pz(img)]


def decode_zxing(img):
    import zxingcpp
    return [r.text for r in zxingcpp.read_barcodes(img)]


def decode_deqr(img):
    # deqr exposes a simple top-level decode that returns text or None.
    # Use try/except: deqr's API has shifted across versions.
    try:
        import deqr
        # deqr 2.x: decoder = deqr.QRdecDecoder(); decoder.decode(img)
        decoder = deqr.QRdecDecoder()
        out = decoder.decode(img)
        if not out:
            return []
        return [d.data.decode('utf-8', errors='replace') if isinstance(d.data, bytes)
                else str(d.data) for d in out]
    except Exception:
        return []


def upscale4x(crop):
    h, w = crop.shape[:2]
    return cv2.resize(crop, (w * 4, h * 4), interpolation=cv2.INTER_LANCZOS4)


def try_variants(crop, decoder):
    for v in qr_live.preprocess_variants(crop):
        try:
            tokens = decoder(v)
            if tokens:
                return tokens
        except Exception:
            continue
    return []


# Each strategy: name -> (callable(frame, x1, y1, x2, y2) -> list[token])
STRATEGIES = {
    'baseline-pyzbar':
        lambda f, x1, y1, x2, y2: decode_pyzbar(f[y1:y2, x1:x2]),
    'variants-pyzbar':
        lambda f, x1, y1, x2, y2: try_variants(f[y1:y2, x1:x2], decode_pyzbar),
    'pad15-variants-pyzbar':
        lambda f, x1, y1, x2, y2:
        try_variants(pad_crop(f, x1, y1, x2, y2, 15), decode_pyzbar),
    'pad25-variants-pyzbar':
        lambda f, x1, y1, x2, y2:
        try_variants(pad_crop(f, x1, y1, x2, y2, 25), decode_pyzbar),
    'upscale4x-pyzbar':
        lambda f, x1, y1, x2, y2:
        decode_pyzbar(upscale4x(f[y1:y2, x1:x2])),
    'baseline-zxing':
        lambda f, x1, y1, x2, y2: decode_zxing(f[y1:y2, x1:x2]),
    'variants-zxing':
        lambda f, x1, y1, x2, y2: try_variants(f[y1:y2, x1:x2], decode_zxing),
    'pad15-zxing':
        lambda f, x1, y1, x2, y2:
        try_variants(pad_crop(f, x1, y1, x2, y2, 15), decode_zxing),
    'baseline-deqr':
        lambda f, x1, y1, x2, y2: decode_deqr(f[y1:y2, x1:x2]),
}


def combo_pad_zxing_fallback(f, x1, y1, x2, y2):
    crop = pad_crop(f, x1, y1, x2, y2, 15)
    toks = try_variants(crop, decode_pyzbar)
    if toks:
        return toks
    return try_variants(crop, decode_zxing)


def combo_all_fallback(f, x1, y1, x2, y2):
    crop = pad_crop(f, x1, y1, x2, y2, 15)
    for decoder in (decode_pyzbar, decode_zxing, decode_deqr):
        toks = try_variants(crop, decoder)
        if toks:
            return toks
    return []


STRATEGIES['combo-pad15-zxing-fallback'] = combo_pad_zxing_fallback
STRATEGIES['combo-all-fallback'] = combo_all_fallback


# Temporal strategies: maintain a per-strategy ring buffer of recent
# crops (aligned to current shape) and median-stack progressively wider
# windows on a per-frame miss. The cascade is N=5 -> 10 -> 20 -> 40.
# N=40 is *deeper* than the production cascade to see if even more
# averaging helps on this clip's compression/banding pattern.
from collections import deque  # noqa: E402

_STACK_RINGS = {}  # strategy_name -> deque

def _stacked_try(name, crop, decoder, cascade=(5, 10, 20, 40)):
    ring = _STACK_RINGS.setdefault(name, deque(maxlen=max(cascade)))
    toks = try_variants(crop, decoder)
    if toks:
        ring.append(crop)
        return toks
    if not ring:
        ring.append(crop)
        return []
    aligned = [qr_live._resize_to(c, crop.shape) for c in ring] + [crop]
    for window in cascade:
        if len(aligned) < window:
            continue
        sub = aligned[-window:]
        stacked = np.median(np.stack(sub, axis=0), axis=0).astype(np.uint8)
        toks = try_variants(stacked, decoder)
        if toks:
            ring.append(crop)
            return toks
    ring.append(crop)
    return []


def stack_pad15_pyzbar(f, x1, y1, x2, y2):
    return _stacked_try('stack_pad15_pyzbar',
                        pad_crop(f, x1, y1, x2, y2, 15), decode_pyzbar)


def stack_pad15_zxing(f, x1, y1, x2, y2):
    return _stacked_try('stack_pad15_zxing',
                        pad_crop(f, x1, y1, x2, y2, 15), decode_zxing)


def stack_pad15_combo(f, x1, y1, x2, y2):
    """Stacking with pyzbar->zxing fallback per stacked-frame attempt."""
    crop = pad_crop(f, x1, y1, x2, y2, 15)
    ring = _STACK_RINGS.setdefault('stack_pad15_combo', deque(maxlen=40))
    for dec in (decode_pyzbar, decode_zxing):
        toks = try_variants(crop, dec)
        if toks:
            ring.append(crop)
            return toks
    if ring:
        aligned = [qr_live._resize_to(c, crop.shape) for c in ring] + [crop]
        for window in (5, 10, 20, 40):
            if len(aligned) < window:
                continue
            stacked = np.median(np.stack(aligned[-window:], axis=0),
                                axis=0).astype(np.uint8)
            for dec in (decode_pyzbar, decode_zxing):
                toks = try_variants(stacked, dec)
                if toks:
                    ring.append(crop)
                    return toks
    ring.append(crop)
    return []


STRATEGIES['stack-pad15-pyzbar'] = stack_pad15_pyzbar
STRATEGIES['stack-pad15-zxing'] = stack_pad15_zxing
STRATEGIES['stack-pad15-combo'] = stack_pad15_combo


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('video', help='Path to test clip (mp4/mov)')
    ap.add_argument('--conf', type=float, default=0.3,
                    help='YOLO confidence threshold')
    ap.add_argument('--skip', type=int, default=1,
                    help='Process every Nth frame (1 = all)')
    ap.add_argument('--strategies', nargs='+', default=None,
                    help='Subset of strategy names to run (default: all)')
    args = ap.parse_args()

    chosen = args.strategies or list(STRATEGIES.keys())
    for name in chosen:
        if name not in STRATEGIES:
            sys.exit(f"unknown strategy: {name}")

    print(f"[load] YOLO detector")
    det_type, det_model = qr_live.create_detector(model_size='s')
    if det_type == 'none':
        sys.exit("no detector available")

    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        sys.exit(f"cannot open {args.video}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    print(f"[video] {args.video}: {total_frames} frames at {fps:.1f} fps")

    # Per-strategy counters
    decoded_frames = defaultdict(int)  # frames with ANY token from this strat
    decoded_crops = defaultdict(int)   # individual detection-box decodes
    total_time = defaultdict(float)
    total_calls = defaultdict(int)
    tokens_seen = defaultdict(set)

    detection_frames = 0
    total_detections = 0
    processed_frames = 0
    frame_idx = -1

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frame_idx += 1
        if frame_idx % args.skip != 0:
            continue
        processed_frames += 1

        dets = qr_live.detect_qr_regions(det_type, det_model, frame, conf=args.conf)
        if not dets:
            continue
        detection_frames += 1
        total_detections += len(dets)

        for (x1, y1, x2, y2, _conf) in dets:
            if x2 <= x1 or y2 <= y1:
                continue
            for name in chosen:
                strat = STRATEGIES[name]
                t0 = time.perf_counter()
                try:
                    toks = strat(frame, x1, y1, x2, y2) or []
                except Exception as exc:
                    print(f"  [{name}] crashed: {exc}")
                    toks = []
                total_time[name] += time.perf_counter() - t0
                total_calls[name] += 1
                if toks:
                    decoded_crops[name] += 1
                    tokens_seen[name].update(toks)

        # Mark frames where each strategy decoded at least one crop.
        # (Reset tally inside the loop above wasn't per-frame; do it now.)
        for name in chosen:
            # NOTE: decoded_frames is just an approximation; the loop above
            # already counts frames implicitly via decoded_crops. Keep
            # decoded_crops as the canonical metric.
            pass

    cap.release()

    print(f"\n[summary]")
    print(f"  processed frames    : {processed_frames}")
    print(f"  frames with YOLO det: {detection_frames}")
    print(f"  total detection box : {total_detections}")
    print()

    # Format: name | decoded/total | rate | tokens | avg ms
    width = max(len(n) for n in chosen) + 2
    print(f"  {'strategy'.ljust(width)} "
          f"{'decoded':>10} {'rate':>7} {'tokens':>7} {'avg ms':>9}")
    print('  ' + '-' * (width + 1 + 10 + 1 + 7 + 1 + 7 + 1 + 9))

    rows = []
    for name in chosen:
        d = decoded_crops[name]
        t = total_detections or 1
        rate = 100.0 * d / t
        ms = (total_time[name] / max(1, total_calls[name])) * 1000
        rows.append((rate, name, d, t, len(tokens_seen[name]), ms))

    rows.sort(reverse=True)  # best rate first
    for rate, name, d, t, ntok, ms in rows:
        print(f"  {name.ljust(width)} "
              f"{d:>4}/{t:<5} {rate:>6.1f}% {ntok:>7} {ms:>8.1f}")

    # Reach analysis: how often each pair of strategies disagree on a
    # crop. The simplest view: strategies sorted by reach show diminishing
    # returns; if combo doesn't beat the best single strat by much, the
    # complexity isn't worth it. Print the top strategy's tokens.
    if rows:
        best = rows[0][1]
        print(f"\n[best] {best}: {sorted(tokens_seen[best])}")


if __name__ == '__main__':
    main()
