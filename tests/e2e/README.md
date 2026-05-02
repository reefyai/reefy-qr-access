# qr-access e2e tests

Pytest + Playwright (headless chromium) covering Buildium sync (API-level) and
dashboard UI rendering. Buildium HTTP is mocked via `responses`, so tests
never touch the real API.

## Setup

```bash
python3 -m venv .venv-e2e
. .venv-e2e/bin/activate
pip install -r tests/e2e/requirements.txt
playwright install chromium
```

## Run

```bash
. .venv-e2e/bin/activate
pytest tests/e2e/ -v
```

API-only (skip the browser tests):

```bash
pytest tests/e2e/test_sync_api.py -v
```

## What's covered

- `test_sync_api.py` (7 tests): first sync creates, second sync is idempotent,
  disappeared user inactivates + tokens revoke, reappeared user reactivates
  but tokens stay revoked, manual users untouched, bad creds rejected without
  saving, version endpoint live.
- `test_dashboard_ui.py` (4 tests): Settings shows version, dashboard renders
  Buildium badges + phone/unit columns, inactive users get the greyed-out
  styling, full Save & Sync flow works through the UI.
