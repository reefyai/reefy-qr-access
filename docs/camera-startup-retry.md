# Camera discovery after delayed network readiness

A startup scan can run before DHCP makes the camera subnet available. QR Access
previously discarded unresolved doors and kept processing zero cameras until a
configuration reload or restart.

Configured doors now remain registered. Their existing reader threads repeat
fresh identity-based discovery with the normal 2, 5, 10, 30, 60, 120 and 300-second
backoff, capped at 300 seconds for further attempts. An unresolved reader never
opens a default/local camera. Working doors keep processing independently.
After resolution, the reader keeps the existing session refresh and authentication
pause behavior. Rejected credentials require a configuration change as before.

ONVIF opener setup uses the matched camera's current address and saved settings.
Discovery does not trigger a door-open action. Reload/shutdown wakes waiting
readers, and canceled generations cannot attach a late camera or opener. Saved
camera identities, credentials, tokens, relay settings and YAML are not rewritten.

## Regression checks

`tests/test_camera_startup_retry.py` runs the production startup loop and reader
threads with synthetic discovery, video-decoder and actuator boundaries. It
requires a ready peer to keep processing, a delayed configured camera to recover,
and the actual Flask snapshot API to change from unavailable to a decoded JPEG
without database or YAML changes. Negative cases cover unrelated camera identities,
late discovery after cancellation, authentication pause, capped retries, transient
network exceptions and dry-run actuator isolation.

`tests/test_onvif_discovery.py` also proves that a later scan re-enumerates interfaces
and probes a camera subnet that was absent during the first scan. Both modules run
in the Dockerfile's existing pre-publication unittest gate.

Repeat with:

```sh
python -m unittest tests.test_camera_startup_retry tests.test_onvif_discovery -v
python -m pytest tests --ignore=tests/e2e/test_dashboard_ui.py -q
```

The same startup cases are run against unchanged application commit
`c6c1e1b883e48cafd4bfba8f0a988894b5fd9965` and the fixed tree. Baseline/fixed logs,
pytest XML and machine-readable provenance are retained under
`test-artifacts/camera-startup/`. These checks simulate network readiness; final
rollout additionally verifies real camera capture and the running image revision.
