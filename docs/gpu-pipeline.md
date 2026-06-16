# GPU-resident inference pipeline

QR Access runs a three-stage pipeline per camera: **decode** (RTSP HEVC ->
frame) -> **detect** (YOLOv8 finds QR regions) -> **QR-decode** (zxing/pyzbar
read the token from each detected crop). The detector dominates CPU - see
`docs/decode-backends.md` for the measurements that established this and
ruled out hardware *decode* as the lever.

This pipeline moves the detector onto the available accelerator. One
"pipeline backend" pairs the decode backend with the detector backend so
all heavy compute stays on one device; only the small detected crop is read
back to host for the CPU-only QR-decode libraries.

| Backend | Decode | Detect | Selected when |
|---|---|---|---|
| `cpu`  | cv2 software | qrdet YOLOv8 / PyTorch-CPU | no accelerator |
| `igpu` | VAAPI        | qrdet YOLOv8 / OpenVINO    | Intel/AMD iGPU (`/dev/dri`, no CUDA) |
| `gpu`  | NVDEC        | qrdet YOLOv8 / TensorRT    | NVIDIA GPU (CUDA) |

Auto-selected at startup (`pipeline.detect_pipeline_backend`); override with
`PIPELINE_BACKEND=cpu|igpu|gpu|auto`. Any hardware path degrades cleanly to
`cpu` (missing driver, failed export, unusable device) and logs once - it
never takes a door offline. The active pipeline is shown in **Settings ->
System -> Detector**.

## Models

`pipeline.py` exports qrdet's YOLOv8 to the accelerated format on demand and
caches it in `$MODEL_CACHE` (`/models`, a persistent volume):

- **OpenVINO IR** (igpu) - portable; can be baked at build via
  `tools/export_models.py --backend openvino`.
- **TensorRT engine** (gpu) - GPU-arch specific, built on first run per
  device (needs onnx/onnxslim, in the image).

An **accuracy gate** confirmed the exported models detect+decode the same
token as PyTorch (see the de-risk in the measurements below).

## Measured results

Full pipeline (decode -> detect -> qr-decode), single synthetic stream,
via `tests/e2e/pipeline_bench.py`. **detect_ms is ~resolution-independent**
(YOLO always runs on a 640 input), so the inference-offload win is the same
at any stream resolution; decode and the QR crop are cheaper at 360p.

### 640x360 (the real camera substream)

| Host | Backend | FPS | CPU (cores) | decode ms | detect ms | qr ms |
|---|---|---|---|---|---|---|
| Intel i5-7260U (iGPU) | cpu  | 2.9  | 1.94 | 2.0 | 327.6 | 12.1 |
| Intel i5-7260U        | igpu | 13.3 | 1.91 | 1.8 | **58.9** | 14.4 |
| NVIDIA RTX 5060 Ti    | cpu  | 15.3 | 4.79 | 0.2 | 58.7 | 6.5 |
| NVIDIA RTX 5060 Ti    | gpu  | 119.0 | 1.20 | 0.3 | **2.3** | 5.5 |

### 720p

| Host | Backend | FPS | CPU (cores) | decode ms | detect ms | qr ms |
|---|---|---|---|---|---|---|
| Intel i5-7260U (iGPU) | cpu  | 2.9  | 1.94 | 2.7 | 319.3 | 19.8 |
| Intel i5-7260U        | igpu | 11.5 | 1.85 | 3.1 | **61.8** | 22.1 |
| NVIDIA RTX 5060 Ti    | cpu  | 13.1 | 4.27 | 0.6 | 65.4 | 10.2 |
| NVIDIA RTX 5060 Ti    | gpu  | 64.2 | 1.35 | 2.1 | **3.6** | 9.6 |

Takeaways: detect is **~5.5x faster on the Intel iGPU** and **~25x on the
NVIDIA GPU**; the discrete GPU runs at **1.2 cores** (vs 4.8 on CPU) with
huge FPS headroom for many cameras. The decode backend is a rounding error
either way - the inference offload is the whole win.

## Perf-regression gate

Baselines live in `tests/e2e/baselines/{intel-igpu,nvidia-gpu}.json`
(measured values + a tolerance). The gate runs the bench on both boxes via
ssh+docker and fails if any config's `detect_ms` or `cpu_cores` exceeds its
baseline tolerance, or the token fails to decode. SSH targets are read from
`tools/perf-boxes.json` (gitignored; copy `tools/perf-boxes.example.json`)
or the `PERF_SSH_*` env vars - device access URLs are never committed:

```bash
python3 tools/run_perf_regression.py            # gate
python3 tools/run_perf_regression.py --update-baselines   # re-record
```

It cannot run in GitHub CI (no GPU, no cameras) - it is a manual/pre-release
gate against the two real boxes.

## Note: GPU-residency vs the simpler swap

`docs/decode-backends.md` measured that keeping the *frame* GPU-resident
(zero-copy decode->inference) adds only marginally over running the
detector on the accelerator with a cheap host frame handoff, because
GPU<->host transfers are small (iGPU shared RAM; NVIDIA PCIe ~0.06-0.58 ms).
This pipeline therefore takes the high-value, robust path: decode on the HW
engine + detect on the accelerator, frame via host, crop read back for QR
decode. The fully zero-copy variant (PyNvVideoCodec / OpenVINO RemoteTensor)
is deferred - it is a marginal, higher-complexity stretch.
