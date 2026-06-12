"""QrTracks unit tests - the per-region decode-latency tracker.

Headline case (observed live on a real door): a laptop on a ledge read
as a permanent ~0.8-conf "QR region", keeping the old global burst
marker armed forever, so grants logged 9-40s decode times for scans
that decoded in 0.2s. With tracks, the laptop owns an eternal
non-decoding track and the phone's track reports its own 0.2s.

Pure stdlib (qr_tracks has no cv2/numpy deps):
    python3 -m unittest tests.test_qr_tracks -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qr_tracks import QrTracks

LAPTOP = (181, 280, 236, 315, 0.85)   # static, never decodes
PHONE_FAR = (358, 209, 391, 250, 0.83)
PHONE_NEAR = (212, 100, 350, 234, 0.96)


class QrTracksTests(unittest.TestCase):
    def test_same_region_keeps_track_id(self):
        tr = QrTracks()
        ids0 = tr.update([LAPTOP], ts=100.0)
        # Slight jitter, same place.
        ids1 = tr.update([(180, 280, 234, 315, 0.80)], ts=100.1)
        self.assertEqual(ids0, ids1)

    def test_distinct_regions_get_distinct_ids(self):
        tr = QrTracks()
        ids = tr.update([LAPTOP, PHONE_FAR], ts=100.0)
        self.assertEqual(len(set(ids)), 2)

    def test_latency_is_first_seen_to_first_decode(self):
        tr = QrTracks()
        (tid,) = tr.update([PHONE_FAR], ts=100.0)
        tr.update([PHONE_FAR], ts=100.1)
        tr.mark_decoded(tid, ts=100.2)
        self.assertEqual(tr.latency_ms(tid), 200)

    def test_repeat_grants_report_original_latency(self):
        # Held-up phone through the event cooldown: later decodes must
        # not move the stamp, so the 2nd grant shows the original
        # presentation latency, not the ~10s cooldown.
        tr = QrTracks()
        (tid,) = tr.update([PHONE_FAR], ts=100.0)
        tr.mark_decoded(tid, ts=100.2)
        for i in range(1, 100):  # 10s of continuous decoding frames
            tr.update([PHONE_FAR], ts=100.0 + i * 0.1)
            tr.mark_decoded(tid, ts=100.0 + i * 0.1)
        self.assertEqual(tr.latency_ms(tid), 200)

    def test_static_nondecoding_region_does_not_inflate_phone(self):
        # The laptop scenario: 40s of laptop-only detections, then the
        # phone appears and decodes in 0.2s - latency must be 200ms,
        # not 40s.
        tr = QrTracks()
        t = 100.0
        while t < 140.0:
            tr.update([LAPTOP], ts=t)
            t += 0.1
        ids = tr.update([LAPTOP, PHONE_FAR], ts=140.0)
        phone_tid = ids[1]
        tr.update([LAPTOP, PHONE_NEAR], ts=140.1)  # raising the phone
        tr.mark_decoded(phone_tid, ts=140.2)
        self.assertEqual(tr.latency_ms(phone_tid), 200)
        # And the laptop's track never reports a latency.
        self.assertIsNone(tr.latency_ms(ids[0]))

    def test_track_expires_after_quiet_ttl(self):
        # Visitor walks away (>5s no detections) and returns: fresh
        # track, fresh measurement.
        tr = QrTracks(ttl_s=5.0)
        (tid0,) = tr.update([PHONE_FAR], ts=100.0)
        tr.mark_decoded(tid0, ts=100.3)
        (tid1,) = tr.update([PHONE_FAR], ts=110.0)  # 10s later
        self.assertNotEqual(tid0, tid1)
        tr.mark_decoded(tid1, ts=110.1)
        self.assertEqual(tr.latency_ms(tid1), 100)

    def test_unknown_or_undecoded_track_latency_none(self):
        tr = QrTracks()
        (tid,) = tr.update([PHONE_FAR], ts=100.0)
        self.assertIsNone(tr.latency_ms(tid))
        self.assertIsNone(tr.latency_ms(9999))

    def test_phone_track_survives_moving_toward_camera(self):
        # Box grows/moves frame to frame as the phone is raised; IoU
        # between consecutive frames keeps it one track.
        tr = QrTracks()
        boxes = [
            (358, 209, 391, 250, 0.83),
            (359, 206, 395, 249, 0.84),
            (361, 204, 399, 250, 0.85),
            (363, 203, 403, 251, 0.87),
        ]
        ids = set()
        for i, b in enumerate(boxes):
            ids.update(tr.update([b], ts=100.0 + i * 0.03))
        self.assertEqual(len(ids), 1)


if __name__ == '__main__':
    unittest.main()
