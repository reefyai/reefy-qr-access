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
- **The payoff therefore scales with resolution and door count.** The
  real motivation is (a) higher-resolution streams and (b) running
  multiple doors, where the container decodes several camera streams
  concurrently and the per-stream decode cost adds up. The before/after
  protocol below measures both the current substream and a 1080p main
  stream so the scaling is visible, not just the small-stream case.

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

### Synthetic multi-camera benchmark

`tests/e2e/multicam_decode.py` proves correctness and measures load
without real cameras: it renders a known QR token onto N synthetic RTSP
streams (HEVC, served via a local mediamtx), decodes each through the
real reader, asserts all N decode the exact token, and measures each
reader's decode CPU via `/proc/<pid>/stat`. Both backends go through the
same ffmpeg reader (cpu = software, vaapi = `-hwaccel vaapi`) so the CPU
numbers are apples-to-apples. It outputs bgr24 frames - exactly what the
detector consumes.

### Measured results (dev device, i5-7260U)

Single-stream, **pure decode** to a null sink (`ffmpeg -benchmark`):

| Stream | SW decode | VAAPI decode |
|---|---|---|
| 640x360 HEVC @10fps | 7.9 %/core | 2.1 %/core (3.7x less) |
| 1920x1080 HEVC @30fps | 26.8 %/core | 17.6 %/core (1.5x less) |

Multi-camera, **real pipeline** (decode -> bgr24 frames, what we
actually run) - VAAPI on the i5-7260U iGPU:

| Streams | CPU backend | VAAPI backend | Result |
|---|---|---|---|
| 4x 640x360 HEVC | 0.78s (6.5 %/core total) | 0.81s (6.8 %/core) | both decode 4/4; **VAAPI ~4% worse** |
| 4x 1920x1080 HEVC | 1.95s | 1.88s (errors) | VAAPI **fails to HW-decode** 4 concurrent 1080p ("internal decoding error"); iGPU surface/session limit |

NVDEC decode decomposition on a discrete **RTX 5060 Ti**, 1080p HEVC,
300 frames, CPU-seconds (`ffmpeg -benchmark`). The explicit
`hevc_cuvid` decoder is used - see the caveat below:

| Path | CPU |
|---|---|
| [1] SW decode -> discard | 1.32 s |
| [2] NVDEC decode -> stays on GPU | **0.18 s** (7x less than SW) |
| [3] SW decode -> bgr24 | 3.81 s |
| [4] NVDEC decode -> bgr24 (readback) | **5.36 s** (*worse* than SW) |

> **Measurement caveat (important).** An earlier run used `-hwaccel
> cuda`, which **silently fell back to software** because the GPU
> container has `NVIDIA_DRIVER_CAPABILITIES=compute,utility` (no `video`
> capability for the generic hwaccel path). That made NVDEC look like a
> no-op. The explicit `-c:v hevc_cuvid` decoder above actually engages
> NVDEC. Lesson: always confirm the HW decoder engaged (`-c:v
> *_cuvid` / verbose logs), don't trust `-hwaccel` to not fall back.

### Conclusion: HW decode is real and cheap; the readback destroys it

1. **HW decode genuinely offloads.** NVDEC decode alone is 7x cheaper on
   CPU than software ([2] 0.18 s vs [1] 1.32 s). VAAPI likewise: the
   first iGPU smoke test showed pure 360p decode 0.18 vs 0.66 s
   (3.7x). The decoders work.
2. **But producing the bgr24 frame the detector needs negates it.** The
   QR-decode libraries are CPU-only and need a bgr24 numpy frame, which
   forces a per-frame GPU->host **download + nv12->bgr24 conversion**.
   That conversion is CPU work common to both backends, and on NVIDIA the
   extra download makes the HW path a **net loss** ([4] 5.36 s vs [3]
   3.81 s). On the iGPU (shared RAM, cheap download) it comes out roughly
   even - either way, no win.
3. **The iGPU also cannot scale high-res** - this Kaby Lake cannot sustain
   4 concurrent 1080p HEVC HW decodes (it errors out); qr-access uses
   640x360 substreams regardless.

Most decisively for the *current* pipeline: **decode is not where
qr-access spends CPU.** Four substreams cost ~6.5 % of one core to decode
either way; the ~195 % container baseline is almost entirely YOLO
inference, which no decode backend can touch.

The only way hardware decode pays off is a **GPU-resident pipeline** -
NVDEC/VAAPI decode whose frame stays on the GPU for inference, with no
bgr24 readback. There the decode+convert (~12.7 ms/frame at 1080p,
[3]/300) leaves the CPU entirely (down to ~0.6 ms/frame, [2]/300). That
is a genuine saving that **scales with resolution and camera count** -
meaningful at 1080p / many streams, small at qr-access's 360p substream
(~1.6 %/core/camera). It is a rearchitecture, not a decode-backend swap.

**Recommendation: do not ship hardware decode as the default.** The
selector, clean CPU fallback, Settings indicator, unit tests, and the
synthetic multi-camera benchmark are kept as the record and as reusable
tooling, but the runtime default stays **cpu**. VAAPI/NVDEC remain
available only as an explicit opt-in (`DECODE_BACKEND=vaapi|nvdec`) for
the niche of a single high-resolution stream on capable hardware.

## The real lever: inference backends (measured)

Hardware *decode* was a dead end, but the same investigation found the
opposite for *inference* - which is where qr-access's CPU actually goes
(the ~195 % baseline is almost all YOLO). Running YOLO through
ONNX-derived runtimes (OpenVINO on Intel, TensorRT on NVIDIA) instead of
PyTorch is a large win, measured on both boxes with stock YOLOv8n @ 640
(a proxy for qr-access's qrdet model):

Intel iGPU box (i5-7260U):

| Backend | Latency | FPS | CPU |
|---|---|---|---|
| PyTorch-CPU (today) | 345 ms | 2.9 | 2.1 cores |
| OpenVINO-CPU | 95 ms | 10.5 | 1.7 cores |
| OpenVINO-iGPU | 18 ms | 55.6 | ~1 core |

NVIDIA box (RTX 5060 Ti):

| Backend | Latency | FPS | CPU |
|---|---|---|---|
| PyTorch-CUDA (today) | 4.5 ms | 225 | ~1 core |
| TensorRT-FP16 | 1.5 ms | 688 | ~1 core |

Takeaways:

- **Intel is the headline**, and it is most of the fleet. OpenVINO on the
  CPU alone is 3.6x faster than PyTorch *for free* (no iGPU); on the iGPU
  it is ~19x faster at ~40x less CPU per frame (0.018 vs 0.72
  core-sec/frame). That directly attacks the inference-bound ~195 %
  baseline - it frees the CPU, or lets a bigger/more-accurate model run
  in the same budget.
- **NVIDIA**: TensorRT is ~3x faster than PyTorch-CUDA, but both already
  clear 200 FPS for a single stream - so it matters for *scale*
  (streams per GPU) and latency headroom, not a single camera.
- **Unified via ONNX**: one ONNX model; ONNX Runtime selects the EP
  (OpenVINO / TensorRT / CPU) per host. ultralytics exports to all three
  directly, so the inference code is one path.

Caveats:

- Numbers are stock YOLOv8n. qr-access uses qrdet's YOLOv8; the export to
  OpenVINO/ONNX and the QR-decode accuracy must be confirmed on the real
  model before relying on these ratios.
- The OpenVINO numbers time the raw network; the PyTorch-CPU number is a
  full `predict` (letterbox + NMS included). End-to-end speedup is large
  but somewhat below the raw ratio because pre/post-processing is shared
  CPU work. The structural win - moving the network off the CPU onto the
  iGPU - holds regardless.

**Recommendation: pursue the inference-backend swap** (OpenVINO on Intel,
TensorRT on NVIDIA, unified via ONNX) as a separate, higher-value
follow-up. It targets the actual bottleneck; the decode work in the rest
of this doc does not.

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

- [x] Plan + measured baseline
- [x] Reader abstraction (vaapi / nvdec / cpu) + detection + clean CPU
      fallback (`video_decode.py`)
- [x] Web UI Decode line + `/api/system` field; unit tests (`tests/test_video_decode.py`)
- [x] Synthetic multi-camera benchmark (`tests/e2e/multicam_decode.py`)
- [x] Measured on dev iGPU (VAAPI) + a discrete GPU (NVDEC) - no benefit
- [x] Decision: do not ship as default; keep as opt-in / record (above)
- [ ] GPU-resident pipeline - researched below; out of scope for now

## Appendix: the GPU-resident pipeline (research)

The body of this doc shows hardware *decode* alone is a no-op for
qr-access because every frame is read back to CPU bgr24 for YOLO and the
CPU-only QR libraries. The only way the hardware actually wins is to
keep the frame on the device and stop reading the whole thing back. This
appendix researches what that takes.

### The shape for qr-access

```
decode on GPU  ->  YOLO on GPU (same memory, no copy)  ->  detected QR
box  ->  download ONLY the small crop  ->  CPU QR decode (zxing/zbar)
```

The full-frame readback (the killer, ~as expensive as software decode)
disappears. Only the detected QR region - small, and only when a code is
present - crosses the bus. The CPU-only QR decode stays, but on a tiny
crop instead of a full 1080p frame.

### NVIDIA path (most proven)

- **Decode -> GPU tensor, zero-copy.** `PyNvVideoCodec` (NVIDIA's
  official Python NVDEC binding) decodes straight to GPU memory and
  exposes frames via DLPack / the CUDA Array Interface, so
  `torch.from_dlpack(frame)` yields a CUDA tensor sharing the same
  memory - no CPU round-trip.
- **Inference on that tensor.** Ultralytics YOLO accepts a CUDA tensor
  as input and keeps it on-device; export to TensorRT (already wired for
  the GPU code path) for speed.
- **QR crop only** is copied to host for zxing-cpp.
- **Or DeepStream** for the industrial multi-camera version: a GStreamer
  pipeline (`nvv4l2decoder` -> `nvstreammux` batching -> `nvinfer`
  TensorRT) that runs N cameras fully GPU-resident. Reported capacity is
  ~4-6 YOLOv8n-INT8 1080p/30fps streams per GPU, ~2x with HEVC, and more
  again when fed camera substreams - i.e. one box scales to many cameras
  instead of hitting the readback wall early.

### Intel iGPU path (for the non-NVIDIA fleet)

The CPU/iGPU devices that make up most of the fleet have no CUDA, so the
NVIDIA decode/inference path does not apply. The Intel-stack equivalent:

- **OpenVINO GPU plugin RemoteTensor API** shares a VAAPI-decoded
  surface with inference with no copy (oneVPL / the `ffmpeg-VAAPI-
  OpenVINO` zero-copy samples demonstrate decode+infer on the iGPU).
- Inference runs under OpenVINO on the Intel iGPU.

### Unifying the inference layer: ONNX Runtime (what Frigate does)

A natural question is whether one inference engine can cover both
vendors. **OpenVINO cannot** - it is Intel-only in practice; the
`openvino_contrib` NVIDIA plugin is out-of-tree, build-from-source, and
not shipped by anyone. Frigate, often cited as "OpenVINO for NVIDIA,"
actually does something better: it standardises on the **ONNX model
format** and selects an **ONNX Runtime execution provider per host** -
OpenVINO EP on Intel, TensorRT/CUDA EP on NVIDIA, CPU EP as fallback.
The NVIDIA detector is the `-tensorrt` image, not OpenVINO.

For qr-access this means the GPU-resident split is less divergent than
"two pipelines":

- **Inference layer - unified.** Export YOLO to ONNX (ultralytics does
  this directly) and run under ONNX Runtime; ORT routes to the best EP
  on each box. One model, one inference API.
- **Decode -> inference zero-copy - still per-vendor.** ORT's CUDA EP
  accepts GPU input via IOBinding (NVDEC -> tensor, zero-copy); Intel's
  zero-copy is OpenVINO's VAAPI RemoteTensor. So the decode handoff
  plumbing still differs even though the model/inference layer is shared.
- **QR decode - unchanged.** Still CPU, on the small detected crop.

Pure-CPU devices still get no hardware path (ORT CPU EP only).

### The QR-decode caveat

There is no mature GPU QR decoder; zxing-cpp / zbar / BoofCV are all CPU.
That is fine here - YOLO already reduces each frame to a small detected
box, so only that crop is read back. QR decode was never the bottleneck;
the full-frame readback was.

### Measured: what residency actually saves (two separate costs)

It is important not to conflate two different transfers:

| Cost | iGPU (i5-7260U) | NVIDIA (RTX 5060 Ti) |
|---|---|---|
| Per-inference **input upload** (640 tensor) | ~0 (shared RAM) | 0.06 ms (1% of inference) |
| Per-frame **decode + nv12->bgr24 convert** | part of the ~6.5%/core (substream) | ~12.7 ms/frame @1080p |

- The **input upload** is genuinely negligible on both platforms (640 is
  small; iGPU shares RAM; NVIDIA PCIe is 0.06 ms). Residency saves almost
  nothing *here*.
- The **decode + convert** is the real cost, and it is *not* negligible
  at high resolution. A GPU-resident pipeline keeps the frame on the GPU,
  so NVDEC/VAAPI decode (cheap) plus a GPU-side colour convert replace
  the CPU decode+convert entirely (~12.7 ms/frame -> ~0.6 ms/frame at
  1080p). At qr-access's 360p substream this is small (~1.6%/core/camera);
  at 1080p / many cameras it is substantial.

So residency *is* worth it for a high-resolution or many-camera GPU
deployment - the earlier "marginal" read came from measuring only the
input upload and missing the decode+convert offload. It remains a
rearchitecture (VAAPI RemoteTensor / PyNvVideoCodec + GPU-side
preprocessing), justified by scale, not by a single substream camera.

### What it buys, and when it is worth it

- **It removes the readback wall**, so a capable GPU box scales to many
  more concurrent (sub)streams - this is a *scale* play (one box, many
  cameras), not a win for a handful of cameras.
- On a few-camera GPU box the CPU is not stressed today, so there is no
  benefit to justify the complexity.
- It is **per-vendor** (CUDA *or* Intel) and a real rearchitecture of
  the detector's ingest + inference, with heavy deps (PyNvVideoCodec /
  DeepStream, or OpenVINO + oneVPL) and model-export maintenance
  (TensorRT / OpenVINO IR).

### Recommendation

Defer. Pursue it only if "one box, many cameras" becomes a product
direction - then the NVIDIA route (PyNvVideoCodec + ultralytics CUDA
tensors, or DeepStream for many streams) is the most proven, with the
OpenVINO RemoteTensor route as the iGPU counterpart. Until then the CPU
pipeline + nano model remains the right fit, and hardware *decode* (the
rest of this doc) stays moot.

### Sources

- PyNvVideoCodec API guide (DLPack / CUDA Array Interface, PyTorch):
  https://docs.nvidia.com/video-technologies/pynvvideocodec/pynvc-api-prog-guide/index.html
- Ultralytics predict (tensor input / device handling):
  https://docs.ultralytics.com/modes/predict
- OpenVINO GPU RemoteTensor API (VAAPI surface sharing, zero-copy):
  https://docs.openvino.ai/2025/openvino-workflow/running-inference/inference-devices-and-modes/gpu-device/remote-tensor-api-gpu-plugin.html
- ffmpeg-VAAPI-OpenVINO zero-copy iGPU decode+infer sample:
  https://github.com/intel-iot-devkit/ffmpeg-VAAPI-OpenVINO
- DeepStream multi-stream capacity / decoder limits (HEVC ~2x, substream
  trick): https://forums.developer.nvidia.com/t/need-help-in-choosing-gpu-for-video-analytics-with-multi-stream-4-rtsp-inputs-outputs/141325
