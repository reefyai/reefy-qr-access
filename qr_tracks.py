"""Per-region detection tracks for decode-latency measurement.

The decode metric used to be one global burst marker: armed at the
first YOLO detection, cleared on grant or after 5s of no detections.
Any persistent QR-shaped object in the scene (a laptop screen on a
ledge, a poster, a sticker) keeps that marker armed forever, so every
grant reported "decode time" that was really "time since the previous
grant" - observed live as 9-40s entries for a scan that decoded in
0.2s.

Tracks fix the attribution: detections are matched frame-to-frame by
IoU into per-region tracks, each carrying its own first-seen and
first-decoded timestamps. A grant reports the latency of the track
that actually decoded; furniture that never decodes accumulates an
eternal track that contaminates nothing.

Pure stdlib (no cv2/numpy) so unit tests run anywhere.
"""


def _iou(a, b):
    """Intersection-over-union of two (x1, y1, x2, y2) boxes."""
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return inter / float(area_a + area_b - inter)


class QrTracks:
    """Greedy IoU tracker over per-frame QR-region detections.

    update() ages out stale tracks and returns one track id per
    detection (existing id when the region overlaps a known track,
    fresh id otherwise). mark_decoded() stamps a track's first
    successful decode; latency_ms() reports first_decoded -
    first_seen, which stays stable across repeat grants of the same
    continuously-visible code.
    """

    def __init__(self, iou_match=0.3, ttl_s=5.0):
        self.iou_match = iou_match
        # Matches the old burst-marker quiet window: a region unseen
        # for this long is a departed visitor; re-appearance starts a
        # fresh measurement.
        self.ttl_s = ttl_s
        self._tracks = {}  # id -> {box, first_seen, last_seen, first_decoded}
        self._next_id = 1

    def update(self, detections, ts):
        """Match `detections` ([(x1, y1, x2, y2, conf), ...]) against
        live tracks at time `ts`. Returns a list of track ids parallel
        to `detections`."""
        # Age out tracks that have been quiet past the TTL.
        for tid in [t for t, tr in self._tracks.items()
                    if ts - tr['last_seen'] > self.ttl_s]:
            del self._tracks[tid]

        ids = []
        claimed = set()
        for det in detections:
            box = tuple(float(v) for v in det[:4])
            best_tid, best_iou = None, self.iou_match
            for tid, tr in self._tracks.items():
                if tid in claimed:
                    continue
                iou = _iou(box, tr['box'])
                if iou >= best_iou:
                    best_tid, best_iou = tid, iou
            if best_tid is None:
                best_tid = self._next_id
                self._next_id += 1
                self._tracks[best_tid] = {
                    'box': box, 'first_seen': ts, 'last_seen': ts,
                    'first_decoded': None,
                }
            else:
                tr = self._tracks[best_tid]
                tr['box'] = box
                tr['last_seen'] = ts
            claimed.add(best_tid)
            ids.append(best_tid)
        return ids

    def mark_decoded(self, track_id, ts):
        """Stamp the first successful decode on a track (later decodes
        keep the original stamp - repeat grants of a held-up phone
        report the original presentation latency, not the cooldown)."""
        tr = self._tracks.get(track_id)
        if tr is not None and tr['first_decoded'] is None:
            tr['first_decoded'] = ts

    def latency_ms(self, track_id):
        """first_decoded - first_seen for the track, in ms; None when
        unknown (track expired or never decoded)."""
        tr = self._tracks.get(track_id)
        if tr is None or tr['first_decoded'] is None:
            return None
        return int(round((tr['first_decoded'] - tr['first_seen']) * 1000))
