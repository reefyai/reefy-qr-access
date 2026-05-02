"""App version, sourced from reefy/app.json (single source of truth)."""

import json
from pathlib import Path


_FALLBACK = '0.0.0-dev'


def get_version() -> str:
    # __file__ -> web/version.py; parent.parent -> repo root
    candidate = Path(__file__).resolve().parent.parent / 'reefy' / 'app.json'
    try:
        return json.loads(candidate.read_text()).get('version') or _FALLBACK
    except (OSError, ValueError):
        return _FALLBACK
