"""Close-to-real tests for the median-stacking QR decode pipeline.

These exercise the ACTUAL decode path - real QR images (generated with
`qrcode`) decoded by the real pyzbar `decode_fn` through
`decode_with_stacking` / `StackingDecoder`. No mocks of the vision code.

Headline case: the stacking `crop_ring` only advances on detection
frames, so between visitors it can stay frozen with the previous
visitor's QR. The next person's hard-to-read scan would then median-stack
those stale crops and decode the previous person's token. StackingDecoder
resets the ring between visitors so each decode reflects only the current
presentation.

Run inside the qr-access runtime image (cv2 + pyzbar + qrcode present):
    cd /app && python -m unittest tests.test_stacking_decoder -v
"""

import unittest
from collections import deque

import cv2
import numpy as np
import qrcode

from qr_live import (
    create_decoder, decode_with_stacking, StackingDecoder, STACK_N,
)

# Same length + ECC -> same QR version -> identical pixel dims, so the
# ring-alignment resize is a no-op and the test is about content, not size.
TOKEN_A = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1"  # 32 chars, like a real token
TOKEN_B = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb2"


def make_qr_bgr(text, box=10, border=4):
    """Render `text` to a real QR as a BGR uint8 image (what the camera
    pipeline feeds decode_with_preprocess)."""
    qr = qrcode.QRCode(
        box_size=box, border=border,
        error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(text)
    qr.make(fit=True)
    pil = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    return cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)


def wreck(bgr):
    """Destroy a QR so no single-frame preprocess variant can read it
    (simulates a hard/blurred scan that forces the stacked-retry path)."""
    h, w = bgr.shape[:2]
    tiny = cv2.resize(bgr, (max(1, w // 9), max(1, h // 9)),
                      interpolation=cv2.INTER_AREA)
    tiny = cv2.GaussianBlur(tiny, (0, 0), sigmaX=3)
    return cv2.resize(tiny, (w, h), interpolation=cv2.INTER_LINEAR)


def speckle(bgr, seed, frac=0.42):
    """Replace `frac` of pixels with random noise - a single frame usually
    fails, but the median of several denoises back to a readable QR."""
    rng = np.random.RandomState(seed)
    out = bgr.copy()
    mask = rng.random(bgr.shape[:2]) < frac
    noise = rng.randint(0, 256, bgr.shape, dtype=np.uint8)
    out[mask] = noise[mask]
    return out


class RealPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # staticmethod: a plain function stored as a class attr would bind
        # `self` on access (self.decode_fn(img) -> decode(self, img)).
        cls.decode_fn = staticmethod(create_decoder())

    def test_clean_qr_decodes(self):
        """Sanity: the real generate->pyzbar pipeline reads a clean QR."""
        toks, used = decode_with_stacking(
            self.decode_fn, make_qr_bgr(TOKEN_A), deque(maxlen=STACK_N))
        self.assertIn(TOKEN_A, toks)
        self.assertFalse(used, "clean QR should decode single-frame, no stack")

    def test_wrecked_qr_does_not_decode_alone(self):
        """The contamination setup is only meaningful if a wrecked QR
        genuinely fails single-frame (forcing the stacked path)."""
        toks, _ = decode_with_stacking(
            self.decode_fn, wreck(make_qr_bgr(TOKEN_B)), deque(maxlen=STACK_N))
        self.assertNotIn(TOKEN_B, toks)

    def test_no_cross_visitor_contamination(self):
        """Visitor A scans (fills the ring), then 15 min later visitor B
        presents an unreadable QR. B must NOT be decoded as A's token."""
        stacker = StackingDecoder(self.decode_fn, gap_s=5.0)
        a = make_qr_bgr(TOKEN_A)
        b = wreck(make_qr_bgr(TOKEN_B))

        t = 1000.0
        last = []
        for i in range(STACK_N):              # visitor A, frames 0.1s apart
            last, _ = stacker.decode(a, t + i * 0.1)
        self.assertIn(TOKEN_A, last, "setup: visitor A should decode")

        toks, _ = stacker.decode(b, t + 900.0)   # visitor B, 15 min later
        self.assertNotIn(
            TOKEN_A, toks,
            "visitor B's scan decoded visitor A's token - cross-visitor "
            "contamination via the stale stacking ring")

    def test_same_visitor_stacking_still_recovers(self):
        """Guard: the reset must not break legitimate same-presentation
        stacking - several noisy frames of ONE visitor still recover."""
        stacker = StackingDecoder(self.decode_fn, gap_s=5.0)
        a = make_qr_bgr(TOKEN_A)
        t = 5000.0
        got = []
        for i in range(STACK_N):              # tight timing, one visitor
            got, _ = stacker.decode(speckle(a, i), t + i * 0.1)
            if TOKEN_A in got:
                break
        self.assertIn(
            TOKEN_A, got,
            "stacking failed to recover a noisy same-visitor QR (the reset "
            "policy is too aggressive)")


if __name__ == '__main__':
    unittest.main()
