# QR Access

GPU-accelerated QR-code door access control. Reads RTSP from one or more
IP cameras, detects QR codes with YOLO + zxing-cpp, validates against a
local user database, and triggers a Shelly relay to unlock the door.

Ships as a single container with a Flask admin UI (user/token management,
camera + door config, live access log, optional Buildium resident sync).

## Why QR codes?

QR codes hit the best balance between convenience, security, and cost
across the common access methods - quick comparison below.

| Option | Convenience / Traceability / Pros / Cons / Security / Cost |
|---|---|
| **Physical key** | **Convenience:** Medium<br>**Traceability:** Low. Usually no record unless paired with camera.<br>**Pros:** Cheap, simple, works without power.<br>**Cons:** Hard to revoke, can be copied/lost, no audit log.<br>**Security:** Low<br>**Cost:** Low |
| **PIN code** | **Convenience:** High<br>**Traceability:** Low to medium. Can log code use if each user has a unique PIN; weak if shared.<br>**Pros:** Easy to use, no card or app needed.<br>**Cons:** Code gets shared, weak identity proof, needs periodic changes.<br>**Security:** Low to medium<br>**Cost:** Low |
| **QR code per resident + video recording** | **Convenience:** High. Can be used from phone, printed card, sticker, or keychain.<br>**Traceability:** High. QR identifies which credential was used; video shows who physically entered.<br>**Pros:** Strong defense in depth, easy to issue/revoke, works well for residents, guests, vendors, kids, and elderly residents. Good access history when paired with logs and camera footage.<br>**Cons:** Static QR can be copied or photographed. Needs camera, scanner, logging, and clear policy for abuse/revocation.<br>**Security:** Medium to high<br>**Cost:** Medium |
| **RFID key fob / card** | **Convenience:** Medium. Tap is easy, but residents must carry a physical item.<br>**Traceability:** Medium to high. Logs which fob/card opened the door; stronger with camera.<br>**Pros:** Fast tap access, reliable, easy to revoke, supports audit logs.<br>**Cons:** Requires carrying a physical item, can be forgotten/lost/shared, replacement management needed.<br>**Security:** Medium to high<br>**Cost:** Medium |
| **Mobile app / NFC** | **Convenience:** Medium to high. Most residents already carry phones, but app setup can be annoying.<br>**Traceability:** High. Logs user/device, time, and door; stronger with camera.<br>**Pros:** No separate fob/card, easy to revoke, good audit logs.<br>**Cons:** App setup friction, phone battery dependency, vendor lock-in.<br>**Security:** High<br>**Cost:** Medium to high |
| **Video intercom** | **Convenience:** Medium. Good for visitors, slower for daily resident access.<br>**Traceability:** High. Can record visitor video/audio, call history, unlock event, and resident/guard approval.<br>**Pros:** Visitor verification, remote unlock, useful for deliveries and unexpected guests.<br>**Cons:** More expensive, slower than credential-based access, not ideal as the only resident access method.<br>**Security:** Medium to high<br>**Cost:** High |
| **Biometric access** | **Convenience:** Medium to high. Nothing to carry, but enrollment and privacy concerns reduce adoption.<br>**Traceability:** High. Logs exact enrolled user and entry time.<br>**Pros:** Hard to share credentials, no key/fob/phone needed.<br>**Cons:** Privacy concerns, enrollment and maintenance overhead.<br>**Security:** High<br>**Cost:** High |

## Architecture

```
RTSP camera (sub-stream)
    │
    ▼  decode      cpu: cv2/ffmpeg   igpu: VAAPI   gpu: NVDEC
raw frame
    │
    ▼  detect      cpu: PyTorch      igpu: OpenVINO   gpu: ONNX Runtime (CUDA)
QR bounding boxes (YOLOv8)
    │
    ▼  decode QR   zxing-cpp / pyzbar (CPU) on the small detected crop only
token
    │
    ▼  token lookup (SQLite)  ──►  Shelly / ONVIF relay unlocks door
    │
    ▼  access_log row + MQTT event
```

Decode + detect form one **pipeline backend** (`cpu` / `igpu` / `gpu`),
auto-selected at startup (NVIDIA → gpu, Intel/AMD iGPU → igpu, else cpu)
and shown in **Settings → System → Detector**. Any hardware path degrades
cleanly to CPU - a missing driver never takes a door offline. Detection
runs on the accelerator; only the small detected crop is read back to the
CPU for the QR-decode libraries.

We measured that **inference, not video decode, dominates CPU** - so the
accelerator is pointed at detection (the gpu path uses ONNX Runtime, not
ultralytics' TensorRT-engine export, to avoid a fragile dependency
chain). See [docs/gpu-pipeline.md](docs/gpu-pipeline.md) and
[docs/decode-backends.md](docs/decode-backends.md) for the full
investigation. The image ships on a `cudnn-runtime` CUDA base (no
toolkit) - ~12 GB, down ~42% from the original ~21 GB.

## Performance

Detection is throttled to a time-based `target_fps` per door (env
`TARGET_FPS`, default 5; this deployment runs 10), independent of the
camera's stream fps. With an accelerated pipeline the detector has ample
headroom, so reaction time is bounded by the frame cadence, not compute.

**Detector latency** (single 640×360 stream, via
`tools/run_perf_regression.py`):

| Backend | Detect / frame | vs CPU |
|---|---|---|
| CPU (PyTorch) — Intel i5-7260U | ~135 ms | 1× |
| **iGPU (OpenVINO)** — Intel i5-7260U | **~27 ms** | ~5× |
| CPU (PyTorch) — Ryzen 5 5600X | ~61 ms | 1× |
| **GPU (ONNX Runtime, CUDA)** — RTX 5060 Ti | **~4 ms** | ~15× |

**End-to-end reaction** (QR enters frame → token decoded), measured live
on the Intel iGPU box at 10 fps: **~100 ms, steady**. That ≈ the 10 fps
frame interval, so reaction is cadence-bound (the detector is only
~27 ms) - a higher-fps camera sub-stream would lower it further.

### Reaction time vs other access methods

QR is usually the *slowest* access method, because it relies on a person
aligning a phone plus a cloud round-trip. By auto-detecting a shown code
at a fixed camera, this system moves QR into the face-recognition / RFID
class:

| Method | Typical reaction |
|---|---|
| RFID / NFC card or fob | ~0.1-0.15 s |
| Face-recognition terminal | ~0.2 s (best <0.1 s) |
| Fingerprint | ~0.5 s |
| BLE / phone unlock | ~0.3-2 s (incl. connection setup) |
| QR — typical systems | ~2-5 s |
| **QR — this system (measured)** | **~0.1 s** |

These are recognition/read times; total door-open adds the lock/relay
actuation, comparable across methods. Figures from vendor specs and
published studies: [Hikvision MinMoe (0.2 s)](https://www.sourcesecurity.com/hikvision-minmoe-face-recognition-terminal-access-control-reader-technical-details.html),
[RFID read time](https://www.rfidjournal.com/ask-the-experts/how-much-time-is-required-to-read-an-rfid-tag/),
[BLE latency](https://pmc.ncbi.nlm.nih.gov/articles/PMC4327007/),
[QR+IoT access study (2.0 s / 5.63 s)](https://www.researchgate.net/publication/353623733_Residential_access_control_system_using_QR_code_and_the_IoT).

## Run (Docker)

```bash
docker run -d --gpus all \
  -p 8080:8080 \
  -v ./config:/app/config \
  -v ./video-logs:/app/video-logs \
  -e QR_ADMIN_PASSWORD=<your-password> \
  ghcr.io/reefyai/reefy-qr-access:latest
```

Then open `http://<host>:8080`, log in, and:

1. **Settings → Available Devices → Scan Network** discovers ONVIF cameras
   and Shelly relays on the LAN.
2. **Settings → Doors → Add Door** pairs a camera to a Shelly relay.
3. **Users → Add User** creates a resident; the system generates a unique
   QR token. Print/share the QR; show it to the camera; the door unlocks.

## Monitoring and alarm emails

QR Access can email building admins when a door's camera goes offline
or its Shelly relay stops responding, and again when the door
recovers. Configure admin emails in **Settings → Monitoring**. See
[docs/monitoring-alarms.md](docs/monitoring-alarms.md) for the full
behaviour (what's checked, how often, what the emails look like, and
how state survives restarts).

## Buildium integration (optional)

If your building uses [Buildium](https://www.buildium.com/) for resident
management, **Settings → Integrations → Buildium** can pull all owners +
tenants on demand. Re-syncing is idempotent: new residents are added,
removed residents are inactivated (their tokens are revoked, but the
historical access log is preserved). See
[docs/buildium-integration.md](docs/buildium-integration.md) for the design.

## Repo layout

```
├── run.py                       # entrypoint: starts web UI + detector loop
├── qr_live.py                   # multi-door RTSP detector + Shelly control
├── web/                         # Flask app
│   ├── app.py                   # routes
│   ├── db.py                    # SQLite schema + helpers
│   ├── services/buildium.py     # Buildium API client + sync orchestrator
│   └── templates/, static/      # Jinja2 + vanilla JS
├── tests/e2e/                   # pytest + Playwright e2e suite
├── docs/                        # design notes
├── reefy/                       # canonical app spec (version, icon) for the
│                                #  Reefy app catalog
└── Dockerfile                   # cudnn-runtime CUDA 12.9 base + GPU deps
```

## Development

```bash
git clone https://github.com/reefyai/reefy-qr-access.git
cd reefy-qr-access
python3 -m venv .venv-e2e
. .venv-e2e/bin/activate
pip install -r tests/e2e/requirements.txt
playwright install chromium
pytest tests/e2e/ -v
```

## Releasing

Bump the `version` and `image` fields in [`reefy/app.json`](reefy/app.json),
commit + push to `main`. GitHub Actions
([`.github/workflows/build.yml`](.github/workflows/build.yml)) reads the new
tag from `reefy/app.json`, builds the image, and pushes to GHCR. Then update
the catalog entry in `reefy-service/apps/qr-access/app.json` to match.
