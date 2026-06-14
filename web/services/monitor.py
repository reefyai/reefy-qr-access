"""Door health monitor: per-door camera + Shelly checks every 60s,
emails admins on alarm and again on recovery.

State (per-door alarm flags, stale-since timestamps) lives in the
app_settings key/value table so it survives container restarts and
device reboots without re-paging admins.

See docs/monitoring-alarms.md for the user-facing description.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone

from .. import db
from . import shelly as shelly_svc
from . import email as email_svc


TICK_INTERVAL_S = 60
BOOT_GRACE_S = 180
CAMERA_STALE_S = 60

DETECTOR_STATUS_PATH = 'config/detector_status.json'

CONFIG_KEY = 'monitor.config'
STATE_KEY_PREFIX = 'monitor.state.door_'


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def default_config() -> dict:
    return {
        'enabled': False,
        'admin_emails': [],
    }


def get_config() -> dict:
    cfg = db.get_setting(CONFIG_KEY) or {}
    merged = default_config()
    merged.update({k: v for k, v in cfg.items() if v is not None})
    # admin_emails is always a list of stripped non-empty strings
    raw_emails = merged.get('admin_emails') or []
    if isinstance(raw_emails, str):
        # tolerate comma- or newline-separated string from older clients
        raw_emails = [p for p in raw_emails.replace(',', '\n').split('\n')]
    merged['admin_emails'] = [
        e.strip() for e in raw_emails if e and e.strip()
    ]
    return merged


def save_config(cfg: dict) -> dict:
    """Persist normalised config. Returns the saved value."""
    emails_raw = cfg.get('admin_emails') or []
    if isinstance(emails_raw, str):
        emails_raw = [p for p in emails_raw.replace(',', '\n').split('\n')]
    emails = []
    seen = set()
    for e in emails_raw:
        e = (e or '').strip()
        if not e or e in seen:
            continue
        seen.add(e)
        emails.append(e)
    payload = {
        'enabled': bool(cfg.get('enabled')),
        'admin_emails': emails,
    }
    db.set_setting(CONFIG_KEY, payload)
    return payload


# ---------------------------------------------------------------------------
# Per-door state
# ---------------------------------------------------------------------------

def _state_key(door_id: int) -> str:
    return f'{STATE_KEY_PREFIX}{door_id}'


def _default_door_state() -> dict:
    return {
        'camera': {'healthy': True, 'stale_since_ts': None,
                    'alarm_active': False, 'last_frame_ts': None},
        'shelly': {'healthy': True, 'alarm_active': False,
                    'last_error': None},
        'last_alarm_at': None,
        'last_recovery_at': None,
    }


def get_door_state(door_id: int) -> dict:
    saved = db.get_setting(_state_key(door_id)) or {}
    base = _default_door_state()
    # shallow merge top-level, then per-component
    for k in ('camera', 'shelly'):
        if isinstance(saved.get(k), dict):
            base[k].update(saved[k])
    for k in ('last_alarm_at', 'last_recovery_at'):
        if k in saved:
            base[k] = saved[k]
    return base


def save_door_state(door_id: int, state: dict) -> None:
    db.set_setting(_state_key(door_id), state)


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def _read_detector_status() -> dict:
    try:
        with open(DETECTOR_STATUS_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def check_camera(door: dict, status_by_name: dict, now_ts: float) -> dict:
    """Returns {'ok': bool, 'last_frame_ts': float|None, 'age_s': float|None,
                'unknown': bool, 'detail': str}.
    unknown=True means we have no data yet (detector hasn't reported)
    - caller treats this as "don't alarm yet".
    """
    entry = status_by_name.get(door['name'])
    if not entry or not entry.get('last_frame_ts'):
        return {'ok': False, 'last_frame_ts': None, 'age_s': None,
                'unknown': True, 'detail': 'no detector status yet'}
    last_ts = float(entry['last_frame_ts'])
    age = now_ts - last_ts
    ok = age <= CAMERA_STALE_S
    return {
        'ok': ok,
        'last_frame_ts': last_ts,
        'age_s': age,
        'unknown': False,
        'detail': 'OK' if ok else f'no frames in {int(age)}s',
    }


def check_shelly(door: dict) -> dict:
    """Relay-leg health, opener-aware. Returns {'ok', 'detail',
    'configured'}. Kept the name for the email/reconcile shape; for an
    ONVIF door it probes the camera's ONVIF relay (GetRelayOutputs)
    instead of a Shelly RPC, so 'Relay unreachable' alarms cover both."""
    opener = (door.get('opener_type') or 'shelly')
    if opener == 'onvif':
        uuid = (door.get('camera_uuid') or '').strip()
        ip = next((c.get('ip') for c in db.get_cameras()
                   if c.get('uuid') == uuid), None)
        if not ip:
            return {'ok': False, 'configured': True,
                    'detail': 'camera IP not found (run Scan Network)'}
        from qr_live import onvif_relay_check
        r = onvif_relay_check(ip, door.get('camera_user', ''),
                              door.get('camera_pass', ''))
        return {
            'ok': bool(r.get('ok')),
            'detail': 'OK' if r.get('ok') else (r.get('error') or 'unreachable'),
            'configured': True,
        }
    if not (door.get('shelly_device_id') or '').strip():
        return {'ok': True, 'detail': 'not configured', 'configured': False}
    r = shelly_svc.check_door(door)
    return {
        'ok': bool(r.get('ok')),
        'detail': 'OK' if r.get('ok') else (r.get('error') or 'unreachable'),
        'configured': True,
    }


# ---------------------------------------------------------------------------
# Email rendering
# ---------------------------------------------------------------------------

def _now_human() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')


def _status_row(label: str, ok: bool, detail: str) -> tuple[str, str]:
    badge_html = ('<span style="color:#2ecc71;font-weight:600;">OK</span>'
                   if ok else
                   f'<span style="color:#e74c3c;font-weight:600;">FAIL</span>')
    html = (f'<tr><td style="padding:4px 12px;color:#888;">{label}</td>'
            f'<td style="padding:4px 12px;">{badge_html}</td>'
            f'<td style="padding:4px 12px;color:#555;">{detail}</td></tr>')
    text = f'  {label:<8} {"OK" if ok else "FAIL"}    {detail}'
    return html, text


def _build_alarm_email(door_name: str, cam: dict, shelly: dict) -> tuple[str, str, str]:
    """Returns (subject, html, text). Alarm = at least one component down."""
    bad = []
    if not cam['ok']:
        bad.append('camera')
    if shelly['configured'] and not shelly['ok']:
        bad.append('relay')
    label = ' + '.join(bad) if bad else 'unknown issue'
    subject = f'[Alarm] Door "{door_name}" - {label} unreachable'

    issue_parts = []
    if not cam['ok']:
        issue_parts.append(f'Camera: {cam["detail"]}.')
    if shelly['configured'] and not shelly['ok']:
        issue_parts.append(f'Relay: {shelly["detail"]}.')
    if cam['ok'] and (shelly['configured'] and shelly['ok']):
        issue_parts.append('Component flapped during this tick.')
    issue = ' '.join(issue_parts)

    cam_row_h, cam_row_t = _status_row('Camera', cam['ok'], cam['detail'])
    if shelly['configured']:
        sh_row_h, sh_row_t = _status_row('Relay', shelly['ok'], shelly['detail'])
    else:
        sh_row_h, sh_row_t = _status_row('Relay', True, 'not configured')

    when = _now_human()
    html = f"""\
<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif;
            max-width:560px;color:#222;">
  <h2 style="color:#c0392b;margin:0 0 0.6em 0;">Door alarm: {door_name}</h2>
  <p style="margin:0 0 0.4em 0;color:#555;">{when}</p>
  <p style="margin:0.8em 0;">{issue}</p>
  <table style="border-collapse:collapse;margin-top:0.6em;font-size:14px;">
    <thead><tr style="border-bottom:1px solid #eee;text-align:left;">
      <th style="padding:4px 12px;color:#888;font-weight:600;">Component</th>
      <th style="padding:4px 12px;color:#888;font-weight:600;">Status</th>
      <th style="padding:4px 12px;color:#888;font-weight:600;">Detail</th>
    </tr></thead>
    <tbody>{cam_row_h}{sh_row_h}</tbody>
  </table>
  <p style="margin-top:1.4em;color:#888;font-size:12px;">
    Sent by QR Access monitoring. You'll receive a follow-up when
    this door recovers.
  </p>
</div>
"""
    text = f"""\
Door alarm: {door_name}
{when}

{issue}

Status:
{cam_row_t}
{sh_row_t}

Sent by QR Access monitoring. You'll receive a follow-up when
this door recovers.
"""
    return subject, html, text


def _build_recovery_email(door_name: str, cam: dict, shelly: dict) -> tuple[str, str, str]:
    subject = f'[Recovered] Door "{door_name}" - back online'
    when = _now_human()
    cam_row_h, cam_row_t = _status_row('Camera', True, cam.get('detail') or 'OK')
    sh_row_h, sh_row_t = _status_row(
        'Relay', True,
        shelly.get('detail') or ('not configured' if not shelly.get('configured') else 'OK'))
    html = f"""\
<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif;
            max-width:560px;color:#222;">
  <h2 style="color:#27ae60;margin:0 0 0.6em 0;">Door recovered: {door_name}</h2>
  <p style="margin:0 0 0.4em 0;color:#555;">{when}</p>
  <p style="margin:0.8em 0;">Both camera and relay are healthy again.</p>
  <table style="border-collapse:collapse;margin-top:0.6em;font-size:14px;">
    <thead><tr style="border-bottom:1px solid #eee;text-align:left;">
      <th style="padding:4px 12px;color:#888;font-weight:600;">Component</th>
      <th style="padding:4px 12px;color:#888;font-weight:600;">Status</th>
      <th style="padding:4px 12px;color:#888;font-weight:600;">Detail</th>
    </tr></thead>
    <tbody>{cam_row_h}{sh_row_h}</tbody>
  </table>
</div>
"""
    text = f"""\
Door recovered: {door_name}
{when}

Both camera and relay are healthy again.

Status:
{cam_row_t}
{sh_row_t}
"""
    return subject, html, text


def build_test_email() -> tuple[str, str, str]:
    when = _now_human()
    subject = '[Test] QR Access monitoring alarm'
    html = f"""\
<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif;
            max-width:560px;color:#222;">
  <h2 style="color:#2980b9;margin:0 0 0.6em 0;">Monitoring test</h2>
  <p style="color:#555;margin:0 0 0.6em 0;">{when}</p>
  <p>This is a test alarm from QR Access. If you're reading it, the
     monitoring email path is working. No door is actually in alarm.</p>
</div>
"""
    text = f"""\
QR Access monitoring test
{when}

This is a test alarm from QR Access. If you're reading it, the
monitoring email path is working. No door is actually in alarm.
"""
    return subject, html, text


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------

def _reconcile(door: dict, cam: dict, shelly: dict, admins: list[str],
                in_grace: bool) -> dict | None:
    """Update saved door state. Returns the email payload to send
    (alarm or recovery), or None if no action."""
    door_id = door['id']
    state = get_door_state(door_id)

    cam_ok = cam['ok'] or cam.get('unknown')  # 'unknown' is not failure
    sh_ok = shelly['ok']
    overall_healthy = cam_ok and sh_ok

    # Refresh stamps regardless of email decisions.
    state['camera']['last_frame_ts'] = cam.get('last_frame_ts')
    state['shelly']['last_error'] = None if sh_ok else shelly['detail']

    payload = None
    was_alarming = bool(state['camera']['alarm_active']
                          or state['shelly']['alarm_active'])

    if overall_healthy:
        state['camera']['healthy'] = True
        state['camera']['stale_since_ts'] = None
        state['shelly']['healthy'] = True
        if was_alarming:
            state['camera']['alarm_active'] = False
            state['shelly']['alarm_active'] = False
            state['last_recovery_at'] = _now_human()
            payload = ('recovery', _build_recovery_email(door['name'], cam, shelly))
    else:
        # mark which legs are unhealthy
        state['camera']['healthy'] = bool(cam_ok)
        state['shelly']['healthy'] = bool(sh_ok)
        if not cam_ok and not state['camera']['stale_since_ts']:
            state['camera']['stale_since_ts'] = time.time()
        if cam_ok:
            state['camera']['stale_since_ts'] = None

        if not was_alarming and not in_grace:
            state['camera']['alarm_active'] = not cam_ok
            state['shelly']['alarm_active'] = not sh_ok
            state['last_alarm_at'] = _now_human()
            payload = ('alarm', _build_alarm_email(door['name'], cam, shelly))

    save_door_state(door_id, state)
    return payload


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

_worker_started = False
_worker_stop = threading.Event()
_started_at: float | None = None


def start_worker() -> None:
    """Idempotent: start the background poll loop once per process."""
    global _worker_started, _started_at
    if _worker_started:
        return
    _worker_started = True
    _started_at = time.time()
    t = threading.Thread(target=_worker_loop, daemon=True, name='door-monitor')
    t.start()


def stop_worker() -> None:
    _worker_stop.set()


def _worker_loop() -> None:
    # Brief startup pause so the Flask app + DB are fully up.
    time.sleep(5)
    while not _worker_stop.is_set():
        try:
            tick()
        except Exception as e:
            print(f'[monitor] tick failed: {e}')
        # Sleep in small increments so stop_worker() is responsive.
        for _ in range(TICK_INTERVAL_S):
            if _worker_stop.is_set():
                return
            time.sleep(1)


def peek() -> dict:
    """Read-only health snapshot. Runs the same checks as a tick but
    does not reconcile state or send emails - safe for the Settings UI
    to call on every page load without racing with the background
    worker."""
    cfg = get_config()
    doors = db.get_doors()
    status_by_name = _read_detector_status()
    now_ts = time.time()
    in_grace = (_started_at is not None
                 and (now_ts - _started_at) < BOOT_GRACE_S)
    per_door = []
    for door in doors:
        cam = check_camera(door, status_by_name, now_ts)
        shelly = check_shelly(door)
        per_door.append({
            'id': door['id'], 'name': door['name'],
            'camera': cam, 'shelly': shelly,
        })
    return {
        'enabled': bool(cfg.get('enabled')),
        'in_grace': in_grace,
        'doors': per_door,
    }


def tick() -> dict:
    """Single monitoring pass. Reconciles state, sends alarm/recovery
    emails. Called by the background worker, NOT by the UI - see
    peek() for the read-only path."""
    cfg = get_config()
    enabled = bool(cfg.get('enabled'))
    admins = cfg.get('admin_emails') or []
    doors = db.get_doors()
    status_by_name = _read_detector_status()
    now_ts = time.time()
    in_grace = (_started_at is not None
                 and (now_ts - _started_at) < BOOT_GRACE_S)

    sent_emails = 0
    per_door = []
    for door in doors:
        cam = check_camera(door, status_by_name, now_ts)
        shelly = check_shelly(door)
        per_door.append({
            'id': door['id'], 'name': door['name'],
            'camera': cam, 'shelly': shelly,
        })

        if not enabled:
            continue

        payload = _reconcile(door, cam, shelly, admins, in_grace)
        if payload is None:
            continue
        kind, (subject, html, text) = payload
        if not admins:
            print(f'[monitor] {kind} for {door["name"]} but no admin '
                  f'emails configured; skipping send')
            continue
        sent, errors = email_svc.send_alert(admins, subject, html, text)
        sent_emails += sent
        for err in errors:
            print(f'[monitor] {kind} send error: {err}')

    return {
        'enabled': enabled,
        'in_grace': in_grace,
        'sent_emails': sent_emails,
        'doors': per_door,
    }
