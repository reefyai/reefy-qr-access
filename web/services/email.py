"""Email delivery: config, queue worker, retry/backoff.

The actual send logic lives in `email_providers/*.py` (SMTP /
AgentMail). This module owns: queue, worker loop, error
classification + retry scheduling, template rendering, dashboard
event bus.

See docs/email-delivery.md (SMTP) and docs/agentmail-integration.md
(provider abstraction + retry/backoff).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from .. import db
from ..events import email_bus
from .email_providers import (
    build_provider, EmailProvider,
    RateLimitError, TransientError, PermanentError,
)


DEFAULT_SUBJECT = 'Your QR access code'
QR_IMG_TAG = ('<img src="cid:qrcode" alt="QR access code" '
              'style="border:1px solid #eee;padding:6px;'
              'background:#fff;max-width:240px;display:block;">')
DEFAULT_BODY_HTML = """\
<p>Hi {full_name},</p>
<p>Here is your QR code for door access. Show it to the camera at the
entrance and the door will unlock.</p>
<p style="margin:1.5em 0;">{qr_code}</p>
<p>Save this image on your phone or print it. Keep it private - anyone
with the image can open the door.</p>
"""
DEFAULT_BODY_TEXT = """\
Hi {full_name},

Your QR code for door access is attached. Show it to the camera at the
entrance and the door will unlock. Save it on your phone or print it.
Keep it private - anyone with the image can open the door.
"""

# Substitutions available in subject + body templates.
TEMPLATE_VARS = ['full_name', 'unit_label', 'building',
                 'email', 'phone', 'qr_code']

# Pace consecutive sends so a 100-in-one-second burst doesn't trip
# spam heuristics on Gmail (and even AgentMail's friendlier limits).
MIN_INTERVAL_S = 1.0

# Backoff schedules. Rate-limit deferrals are hour-scale because at
# 100/day the queue naturally drains over the next day - tight retries
# would burn quota. Transient errors get minute-scale retries; permanent
# errors don't retry at all.
RATE_LIMIT_BACKOFF_S = [15 * 60, 60 * 60, 4 * 60 * 60,
                         12 * 60 * 60, 24 * 60 * 60]
TRANSIENT_BACKOFF_S = [30, 2 * 60, 10 * 60, 60 * 60]
MAX_TRANSIENT_ATTEMPTS = len(TRANSIENT_BACKOFF_S) + 1


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

def _render_template(tmpl: str, ctx: dict) -> str:
    """Substitute {{ var }} and {var} placeholders. Unknown placeholders
    are left as-is rather than raising, so a typo in admin's template
    doesn't break the send."""
    out = tmpl
    for k, v in ctx.items():
        out = out.replace('{{ ' + k + ' }}', str(v))
        out = out.replace('{{' + k + '}}', str(v))
        out = out.replace('{' + k + '}', str(v))
    return out


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def get_config() -> dict | None:
    cfg = db.get_setting('email.config')
    return cfg if isinstance(cfg, dict) else None


def is_configured() -> bool:
    c = get_config() or {}
    provider = (c.get('provider') or 'smtp').lower()
    if provider == 'smtp':
        return bool(c.get('smtp_host') and c.get('username')
                     and c.get('password') and c.get('from_email'))
    if provider == 'agentmail':
        return bool(c.get('agentmail_api_key'))
    return False


def public_status() -> dict:
    """Status payload safe to expose - never returns the password
    or API key. Includes provider-specific masked context so the
    Settings UI can render the right form."""
    c = get_config() or {}
    provider = (c.get('provider') or 'smtp').lower()
    return {
        'configured': is_configured(),
        'provider': provider,
        # SMTP fields
        'smtp_host': c.get('smtp_host', ''),
        'smtp_port': c.get('smtp_port', 587),
        'username': c.get('username', ''),
        'has_password': bool(c.get('password')),
        # AgentMail fields
        'agentmail_inbox_id': c.get('agentmail_inbox_id', ''),
        'has_agentmail_api_key': bool(c.get('agentmail_api_key')),
        # Shared
        'from_email': c.get('from_email', ''),
        'from_name': c.get('from_name', ''),
        'subject_template': c.get('subject_template', ''),
        'body_html_template': c.get('body_html_template', ''),
        'body_text_template': c.get('body_text_template', ''),
        'default_subject': DEFAULT_SUBJECT,
        'default_body_html': DEFAULT_BODY_HTML,
        'default_body_text': DEFAULT_BODY_TEXT,
        'template_vars': TEMPLATE_VARS,
    }


def _on_inbox_provisioned(inbox_id: str) -> None:
    """Callback for AgentMailProvider when it auto-creates an inbox.
    Persists the id so future sends reuse it."""
    cfg = get_config() or {}
    if cfg.get('agentmail_inbox_id') == inbox_id:
        return
    cfg = dict(cfg)
    cfg['agentmail_inbox_id'] = inbox_id
    db.set_setting('email.config', cfg)


def _provider_for(cfg: dict) -> EmailProvider:
    return build_provider(cfg, on_inbox_provisioned=_on_inbox_provisioned)


# ---------------------------------------------------------------------------
# Test send (called by the Send Test button in Settings -> Email)
# ---------------------------------------------------------------------------

def send_test(cfg: dict, to_email: str) -> None:
    _provider_for(cfg).send_test(to_email)


# ---------------------------------------------------------------------------
# Job queue
# ---------------------------------------------------------------------------

def enqueue_for_user(user_id: int) -> int | None:
    """Insert an email_jobs row + token snapshot for a user. Returns
    the job id, or None if the user has no email or no active token."""
    user = db.get_user(user_id)
    if not user or not user.get('email'):
        return None
    tokens = db.get_user_tokens(user_id)
    active = [t for t in tokens if t['active']]
    if not active:
        return None
    token = active[0]
    conn = db.get_db()
    cur = conn.execute(
        "INSERT INTO email_jobs (user_id, token_id, to_email, subject) "
        "VALUES (?, ?, ?, ?)",
        (user_id, token['id'], user['email'], DEFAULT_SUBJECT))
    db.commit()
    job_id = cur.lastrowid
    email_bus.publish({'type': 'email-job', 'id': job_id, 'user_id': user_id,
                        'status': 'queued'})
    return job_id


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

def _process_job(job: dict) -> None:
    """Mark sending, send via provider, mark sent/failed/retrying.
    Publishes state transitions on the email_bus."""
    job_id = job['id']
    user_id = job['user_id']
    cfg = get_config()
    if not cfg:
        _mark_failed(job_id, user_id, 'Email integration not configured')
        return
    user = db.get_user(user_id)
    if not user or not user.get('email'):
        _mark_failed(job_id, user_id, 'User missing email')
        return

    conn = db.get_db()
    conn.execute(
        "UPDATE email_jobs SET status='sending', started_at=datetime('now') "
        "WHERE id=?", (job_id,))
    db.commit()
    email_bus.publish({'type': 'email-job', 'id': job_id, 'user_id': user_id,
                        'status': 'sending'})

    # Render the QR PNG fresh from the token.
    from ..qr_utils import get_qr_path, generate_qr_png
    token_row = conn.execute(
        "SELECT token FROM tokens WHERE id=?", (job['token_id'],)).fetchone()
    if not token_row:
        _mark_failed(job_id, user_id, 'Token vanished')
        return
    token_str = token_row['token']
    png_path = get_qr_path(token_str)
    if not png_path.exists():
        generate_qr_png(token_str)
    qr_bytes = Path(png_path).read_bytes()

    # Build the per-user template context.
    users_full = db.get_users()
    user_full = next((u for u in users_full if u['id'] == user_id), {})
    ctx = {
        'full_name': user.get('full_name') or '',
        'email': user.get('email') or '',
        'phone': user_full.get('phone_primary') or user.get('phone') or '',
        'unit_label': user_full.get('unit_label') or user.get('address') or '',
        'building': user_full.get('building') or '',
        'qr_code': QR_IMG_TAG,
    }
    subject_tmpl = (cfg.get('subject_template') or '').strip() or DEFAULT_SUBJECT
    body_html_tmpl = (cfg.get('body_html_template') or '').strip() or DEFAULT_BODY_HTML
    body_text_tmpl = (cfg.get('body_text_template') or '').strip() or DEFAULT_BODY_TEXT

    subject = _render_template(subject_tmpl, ctx)
    body_html = _render_template(body_html_tmpl, ctx)
    text_ctx = {**ctx, 'qr_code': '[QR code attached as image]'}
    body_text = _render_template(body_text_tmpl, text_ctx)
    if 'cid:qrcode' not in body_html:
        body_html += f'<p style="margin:1.5em 0;">{QR_IMG_TAG}</p>'

    # Send via the configured provider. Errors are classified by the
    # provider into RateLimit/Transient/Permanent so we can pick the
    # right backoff (or give up).
    try:
        provider = _provider_for(cfg)
        provider.send_one(user['email'], subject, body_html, body_text, qr_bytes)
    except RateLimitError as e:
        _defer_for_retry(job, RATE_LIMIT_BACKOFF_S, str(e), kind='rate-limit')
        return
    except TransientError as e:
        _defer_for_retry(job, TRANSIENT_BACKOFF_S, str(e), kind='transient')
        return
    except PermanentError as e:
        _mark_failed(job_id, user_id, str(e)[:500])
        return
    except Exception as e:
        # Unknown exception class - treat as transient (worth retrying)
        # but log so we can promote to a known classification later.
        _defer_for_retry(job, TRANSIENT_BACKOFF_S, str(e), kind='unknown')
        return

    conn.execute(
        "UPDATE email_jobs SET status='sent', finished_at=datetime('now'), "
        "error=NULL WHERE id=?", (job_id,))
    conn.execute(
        "UPDATE users SET last_email_sent_at=datetime('now') WHERE id=?",
        (user_id,))
    db.commit()
    email_bus.publish({'type': 'email-job', 'id': job_id, 'user_id': user_id,
                        'status': 'sent', 'sent_at': db._now()})


def _defer_for_retry(job: dict, schedule: list, error: str,
                       kind: str) -> None:
    """Bump attempts, set next_retry_at = now + schedule[attempts-1].
    For RateLimit: keep deferring forever (the cap clears daily). For
    transient: give up after MAX_TRANSIENT_ATTEMPTS and mark failed."""
    job_id = job['id']
    user_id = job['user_id']
    conn = db.get_db()
    new_attempts = (job.get('attempts') or 0) + 1

    is_transient = (kind in ('transient', 'unknown'))
    if is_transient and new_attempts >= MAX_TRANSIENT_ATTEMPTS:
        # Stamp the final attempts count too - otherwise DB shows
        # MAX-1 and the operator can't tell from the row alone how
        # many tries we burned.
        conn.execute(
            "UPDATE email_jobs SET attempts=? WHERE id=?",
            (new_attempts, job_id))
        db.commit()
        _mark_failed(job_id, user_id,
                      f'after {new_attempts} attempts: {error[:400]}')
        return

    backoff = schedule[min(new_attempts - 1, len(schedule) - 1)]
    conn.execute(
        "UPDATE email_jobs SET status='queued', attempts=?, "
        "next_retry_at=datetime('now', ? || ' seconds'), error=? "
        "WHERE id=?",
        (new_attempts, f'+{backoff}', error[:500], job_id))
    db.commit()
    email_bus.publish({
        'type': 'email-job', 'id': job_id, 'user_id': user_id,
        'status': 'retrying', 'attempts': new_attempts,
        'retry_in_s': backoff, 'kind': kind, 'error': error[:200],
    })


def _mark_failed(job_id: int, user_id: int, error: str) -> None:
    conn = db.get_db()
    conn.execute(
        "UPDATE email_jobs SET status='failed', finished_at=datetime('now'), "
        "error=? WHERE id=?", (error, job_id))
    db.commit()
    email_bus.publish({'type': 'email-job', 'id': job_id, 'user_id': user_id,
                        'status': 'failed', 'error': error})


_worker_started = False
_worker_stop = threading.Event()


def start_worker() -> None:
    """Idempotent: start the background poll loop once per process."""
    global _worker_started
    if _worker_started:
        return
    _worker_started = True
    t = threading.Thread(target=_worker_loop, daemon=True, name='email-worker')
    t.start()


def stop_worker() -> None:
    _worker_stop.set()


def _worker_loop() -> None:
    last_send = 0.0
    while not _worker_stop.is_set():
        try:
            job = _pop_next_queued_job()
        except Exception:
            job = None
        if job is None:
            time.sleep(2)
            continue
        # Pace consecutive sends.
        elapsed = time.monotonic() - last_send
        if elapsed < MIN_INTERVAL_S:
            time.sleep(MIN_INTERVAL_S - elapsed)
        try:
            _process_job(job)
        except Exception as e:
            try:
                _mark_failed(job['id'], job['user_id'], f'worker error: {e}')
            except Exception:
                pass
        last_send = time.monotonic()


def _pop_next_queued_job() -> dict | None:
    """Pop the oldest queued job whose retry timer has elapsed (or
    that has never been retried). Survives container restart because
    state lives in SQLite."""
    conn = db.get_db()
    row = conn.execute(
        "SELECT id, user_id, token_id, to_email, attempts, next_retry_at "
        "FROM email_jobs WHERE status='queued' "
        "AND (next_retry_at IS NULL OR next_retry_at <= datetime('now')) "
        "ORDER BY id LIMIT 1"
    ).fetchone()
    return dict(row) if row else None
