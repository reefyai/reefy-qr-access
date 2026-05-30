"""Shelly RPC probe shared by /api/test-shelly and the health monitor.

A single source of truth for "is this door's Shelly reachable right
now?". Returns a small dict so callers (route handler, monitor) can
decide what to render or whether to alarm.
"""

from __future__ import annotations

import requests
from requests.auth import HTTPDigestAuth

from .. import db


def _resolve_ip(shelly_device_id: str) -> str | None:
    for s in db.get_shellys():
        if s['device_id'] == shelly_device_id:
            return s['ip']
    return None


def check_door(door: dict, timeout: float = 5.0) -> dict:
    """Probe the Shelly relay paired to `door`. Returns:
        {'ok': bool, 'ip': str | None, 'error': str | None, 'status': dict | None}

    ok=False covers: door has no Shelly configured, IP not in the
    discovered-devices table, network error, auth error, non-200, any
    exception from requests. The monitor treats any ok=False as a
    failure - no debounce, by design.
    """
    shelly_id = (door.get('shelly_device_id') or '').strip()
    if not shelly_id:
        return {'ok': False, 'ip': None,
                'error': 'No Shelly configured for this door',
                'status': None}

    ip = _resolve_ip(shelly_id)
    if not ip:
        return {'ok': False, 'ip': None,
                'error': f'Shelly {shelly_id} IP not found. Run Scan Network first.',
                'status': None}

    pwd = door.get('shelly_pass') or ''
    auth = HTTPDigestAuth('admin', pwd) if pwd else None
    try:
        resp = requests.get(f'http://{ip}/rpc/Switch.GetStatus?id=0',
                            auth=auth, timeout=timeout)
        resp.raise_for_status()
        return {'ok': True, 'ip': ip, 'error': None,
                'status': resp.json()}
    except Exception as e:
        return {'ok': False, 'ip': ip,
                'error': f'{type(e).__name__}: {e}'[:300],
                'status': None}
