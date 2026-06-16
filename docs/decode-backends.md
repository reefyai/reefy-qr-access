# Hardware-accelerated video decode (VAAPI / NVDEC / CPU)

QR Access pulls an RTSP stream from each camera and decodes every frame
(H.264 or H.265) before the detector looks for QR codes. Today that
decode happens entirely on the CPU. On a GPU-less edge box, and even
more so once several apps each pull their own stream, that software
decode is pure, avoidable CPU load: every modern Intel iGPU and every
NVIDIA card has a dedicated, fixed-function video-decode block that sits
idle while the CPU does the work by hand.

This document is the plan for adding a **decode-backend selector** with
three options, auto-selected at startup:

| Backend | Hardware | When it's used |
|---|---|---|
| **NVDEC** | NVIDIA GPU (CUDA) | An NVIDIA GPU is present |
| **VAAPI** | Intel / AMD iGPU (`/dev/dri`) | No NVIDIA GPU, but a render node is available |
| **CPU** | none (software libav) | Fallback - always works |

This mirrors the existing `HAS_GPU` switch that already drives the
detector model size and the clip *encoder* (`h264_nvenc` vs `libx264`).
Decode is the last piece still hardwired to the CPU.

## Why we decode on the CPU today

Three things in the current build block hardware decode - all three
must be fixed:

1. **The decoder can't do it.** Ingest is
   `cv2.VideoCapture(url, cv2.CAP_FFMPEG)` (`qr_live.py`). OpenCV here is
   the `opencv-python-headless` pip wheel, which bundles its **own**
   ffmpeg compiled **without** VAAPI / Quick Sync. The apt `ffmpeg` in
   the image *does* have VAAPI, but OpenCV never uses the system one, so
   you cannot fix this by passing `hwaccel` options to the existing
   `VideoCapture`.
2. **The device isn't in the container.** `app.json` sets `gpu: true`
   but has no `devices` entry, so `/dev/dri/renderD128` (the Intel
   render node) is not visible inside the container. Measured on the dev
   device: the node exists on the host but `NO_DRI_IN_CONTAINER`.
3. **No driver.** The image (built on `nvidia/cuda`) ships no
   `intel-media-driver` / `libva` for the iGPU.

## Measured baseline (before)

Captured on the dev device (Intel Core i5-7260U, "Kaby Lake", 4 logical
cores, single camera) running the current software-decode build:

| Metric | Value |
|---|---|
| Stream | 640x360 @ 10 FPS, **HEVC (H.265)** |
| Detector | YOLOv8-n, CPU, ~3.3 FPS processed |
| Container CPU (avg over ~40 s) | **~195 %** (≈ 2.0 of 4 cores) |
| Container RAM | ~590 MiB |
| Decode threads | dedicated `av:hevc` libav threads visible in `top -H` |

Two honest caveats this baseline makes clear:

- **At 640x360 the decode share is modest.** Most of the ~195 % is YOLO
  inference + QR-decode libraries, not video decode. The headline
  "~10-20 % of a core per 1080p stream" figure is a **1080p** number;
  decode cost scales with pixels x fps.
- **The payoff therefore scales with resolution and stream count.** The
  real motivation is (a) higher-resolution streams and (b) the future
  where multiple apps each decode the same camera (see the camera-hub
  discussion). The before/after protocol below measures both the current
  substream and a 1080p main stream so the scaling is visible, not just
  the small-stream case.

## Design

### Decode via PyAV, not OpenCV

Move stream *ingest* to **PyAV** (`av`), which links the **system**
libav. PyAV exposes ffmpeg's hardware-decode path directly
(`hwaccel='vaapi'` or `'cuda'`), decodes into a hardware surface, and we
download to a numpy BGR frame for YOLO. OpenCV stays for everything else
(drawing, image ops); only the ~30 lines of capture/`read()` move.

Rejected alternative: rebuild OpenCV against system ffmpeg, or use the
apt `python3-opencv` + `gstreamer-vaapi` pipeline. Both are heavier and
more fragile than PyAV linking the system libav we already trust.

### Backend auto-selection

A `_detect_decode_backend()` probe, run once at startup, parallel to
`_detect_gpu()`:

```
NVDEC   if torch.cuda.is_available()                       (NVIDIA box)
VAAPI   elif /dev/dri/renderD128 exists and libva loads    (Intel/AMD)
CPU     else                                               (always works)
```

Overridable with `DECODE_BACKEND=auto|vaapi|nvdec|cpu` (env, plus a
`--decode-backend` arg in `run.py`, same shape as `--model-size`).

### Clean fallback

Hardware decode must **degrade, never crash**. If the selected backend
fails to open the stream (missing driver, unsupported codec/profile,
device busy), log one clear warning and fall back to CPU decode for that
stream. A bad iGPU driver must never take the door offline.

### Frame path

Keep the existing `FrameGrabber` ring-buffer, reconnect, and URL-refresh
logic untouched. Only the "open stream + read one frame" core swaps from
`cv2.VideoCapture` to a small `StreamDecoder` abstraction with `vaapi` /
`nvdec` / `cpu` implementations behind one `read() -> (ok, frame)` API.

## Container changes

- **`app.json`**: add `"devices": ["/dev/dri"]` (a restricted field per
  APP-SPEC; passes the render node into the container). NVDEC reuses the
  existing GPU plumbing.
- **`Dockerfile`**: add `libva2`, `intel-media-driver`, `vainfo`
  (debug), and `pyav` to the pip set. Keep the CUDA base so NVDEC still
  works on NVIDIA boxes.

## Web UI: show the active decoder

Settings -> System already has a **Detector** panel (compute, CPU/GPU
model, backend, model, encoder, FPS). Add a **Decode** line next to
**Encoder**:

- `Decode: VAAPI (Intel iGPU)` / `Decode: NVDEC (NVIDIA ...)` /
  `Decode: CPU (software)`

Plumbing: `DETECTOR_INFO` gains `decode_backend` + `decode_device`
fields, surfaced by the existing `/api/system` endpoint and rendered in
`web/templates/settings.html`'s Detector panel. This makes "are we
actually using the iGPU?" answerable from the dashboard, not by SSH.

## Measurement protocol (before / after)

Same camera, same model (nano), same `skip`, detector warmed up, no
other load. For each backend:

1. **Total CPU**: `docker stats --no-stream` sampled 8x over 40 s, averaged.
2. **Decode attribution**: `top -bH -n2 -d 2` inside the container -
   record the `av:*` (decode) thread CPU vs the `python3` (inference)
   threads, so we see the decode delta specifically, not just the total.
3. **FPS sanity**: detector `[STATUS]` FPS must hold (~3.3) - hardware
   decode should not change throughput, only cost.
4. **RAM**: container `MemUsage`.

Run the matrix at **two resolutions** to show the scaling:

| Stream | Backend | Total CPU | Decode-thread CPU | FPS | RAM |
|---|---|---|---|---|---|
| 640x360 HEVC | CPU (before) | ~195 % | _measure_ | 3.3 | 590 MiB |
| 640x360 HEVC | VAAPI (after) | _measure_ | _measure_ | _expect 3.3_ | _measure_ |
| 1080p main | CPU (before) | _measure_ | _measure_ | _measure_ | _measure_ |
| 1080p main | VAAPI (after) | _measure_ | _measure_ | _expect ~3.3_ | _measure_ |

Expectation: at 640x360 the total drops modestly (decode is a small
slice); at 1080p the decode slice is large and VAAPI takes it to near
zero, freeing roughly the difference. The 1080p row is the one that
justifies the work, and it foreshadows the multi-app camera-hub case.

## Risks and notes

- **HW-surface -> numpy download cost.** Hardware decode lands the frame
  in GPU memory; we copy it back to a CPU numpy array for YOLO. This
  copy is real but far cheaper than software decode at 1080p+. Measure
  it; it's part of the "after" number.
- **Codec/profile support varies.** Kaby Lake does H.264 and HEVC 8-bit
  in hardware; 10-bit HEVC may not. The clean-fallback path covers
  unsupported profiles.
- **`devices: /dev/dri` is a restricted field** - the platform must
  allow it for the app. NVDEC needs no new field beyond existing GPU
  support.
- **Driver variance.** `intel-media-driver` (iHD) vs the older `i965`;
  we target iHD. `vainfo` in the image helps diagnose.

## Rollout

1. Implement decoder abstraction + detection + clean fallback.
2. `app.json` `devices` + `Dockerfile` drivers + PyAV.
3. Web UI Decode line + `/api/system` field.
4. Build, publish to **dev** catalog, update the dev device.
5. Run the before/after matrix on the dev device; fill the table above.
6. Promote to prod only after the dev numbers and a clean fallback are
   confirmed.

## Status

- [x] Plan + measured baseline (this doc)
- [ ] `StreamDecoder` abstraction (vaapi / nvdec / cpu) + detection
- [ ] Clean CPU fallback on hardware-decode failure
- [ ] `app.json` `devices` + `Dockerfile` drivers + PyAV
- [ ] Web UI Decode line + `/api/system` field
- [ ] Before/after measurement matrix filled on the dev device
- [ ] Promote to prod
