"""SMTP email delivery: config, queue worker, send-one helper.

See docs/email-delivery.md for design. v1 scope:
 - per-user `[Email]` button enqueues a job
 - background worker picks queued jobs FIFO, sends via SMTP, posts state
   transitions to the email_bus (-> SSE -> dashboard)
 - hardcoded HTML body with CID-embedded QR PNG (no template UI yet)
"""

from __future__ import annotations

import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

from .. import db
from ..events import email_bus


DEFAULT_SUBJECT = 'Your QR access code'
QR_IMG_TAG = ('<img src="cid:qrcode" alt="QR access code" '
              'style="border:1px solid #eee;padding:6px;'
              'background:#fff;max-width:240px;">')
DEFAULT_BODY_HTML = """\
<p>Hi {full_name},</p>
<p>Here is your QR code for door access. Show it to the camera at the
entrance and the door will unlock.</p>
<p style="text-align:center;margin:1.5em 0;">{qr_code}</p>
<p>Save this image on your phone or print it. Keep it private - anyone
with the image can open the door.</p>
"""
DEFAULT_BODY_TEXT = """\
Hi {full_name},

Your QR code for door access is attached. Show it to the camera at the
entrance and the door will unlock. Save it on your phone or print it.
Keep it private - anyone with the image can open the door.
"""

# Substitutions available in subject + body templates. Admins can write
# {{ full_name }} (or {full_name}, both work) anywhere in their subject
# or body to interpolate per-recipient. {{ qr_code }} expands to an
# inline <img cid:qrcode> tag in the HTML body; if omitted the worker
# appends the image at the end so the QR is always present.
TEMPLATE_VARS = ['full_name', 'unit_label', 'building',
                 'email', 'phone', 'qr_code']


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

# Pace SMTP sends so a 100-in-one-second burst doesn't trip provider
# spam heuristics (Gmail in particular). docs/email-delivery.md.
MIN_INTERVAL_S = 1.0


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def get_config() -> dict | None:
    cfg = db.get_setting('email.config')
    return cfg if isinstance(cfg, dict) else None


def is_configured() -> bool:
    c = get_config() or {}
    return bool(c.get('smtp_host') and c.get('username')
                and c.get('password') and c.get('from_email'))


def public_status() -> dict:
    """Status payload safe to expose via GET /api/integrations/email -
    never returns the password."""
    c = get_config() or {}
    return {
        'configured': is_configured(),
        'smtp_host': c.get('smtp_host', ''),
        'smtp_port': c.get('smtp_port', 587),
        'username': c.get('username', ''),
        'from_email': c.get('from_email', ''),
        'from_name': c.get('from_name', ''),
        'has_password': bool(c.get('password')),
        'subject_template': c.get('subject_template', ''),
        'body_html_template': c.get('body_html_template', ''),
        'body_text_template': c.get('body_text_template', ''),
        'default_subject': DEFAULT_SUBJECT,
        'default_body_html': DEFAULT_BODY_HTML,
        'default_body_text': DEFAULT_BODY_TEXT,
        'template_vars': TEMPLATE_VARS,
    }


# ---------------------------------------------------------------------------
# Send one
# ---------------------------------------------------------------------------

def _build_message(cfg: dict, to_email: str, subject: str,
                    body_html: str, body_text: str,
                    qr_png_bytes: bytes) -> EmailMessage:
    msg = EmailMessage()
    msg['From'] = formataddr((cfg.get('from_name') or '', cfg['from_email']))
    msg['To'] = to_email
    msg['Subject'] = subject
    if cfg.get('reply_to'):
        msg['Reply-To'] = cfg['reply_to']
    msg.set_content(body_text)
    msg.add_alternative(body_html, subtype='html')
    # Attach the QR PNG with a Content-ID so the HTML <img src="cid:qrcode">
    # renders inline (better deliverability than a bare attachment).
    html_part = msg.get_payload()[1]
    html_part.add_related(qr_png_bytes, 'image', 'png', cid='<qrcode>')
    return msg


def send_one(cfg: dict, to_email: str, subject: str,
              body_html: str, body_text: str,
              qr_png_bytes: bytes) -> None:
    """Synchronous SMTP send. Raises on failure."""
    msg = _build_message(cfg, to_email, subject, body_html, body_text, qr_png_bytes)
    host = cfg['smtp_host']
    port = int(cfg.get('smtp_port', 587))
    ctx = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls(context=ctx)
        smtp.login(cfg['username'], cfg['password'])
        smtp.send_message(msg)


def send_test(cfg: dict, to_email: str) -> None:
    """Probe send used by the Test Connection / Send Test button.
    Doesn't touch the queue or DB."""
    sample = ('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAA'
              'AAYAAjCB0C8AAAAASUVORK5CYII=')
    import base64
    png = base64.b64decode(sample)
    send_one(cfg, to_email,
              subject='qr-access SMTP test',
              body_html='<p>SMTP relay configured correctly. This is a test.</p>',
              body_text='SMTP relay configured correctly. This is a test.',
              qr_png_bytes=png)


# ---------------------------------------------------------------------------
# Job queue + worker
# ---------------------------------------------------------------------------

def enqueue_for_user(user_id: int) -> int | None:
    """Insert an email_jobs row + token snapshot for a user. Returns the
    job id, or None if the user has no email or no active token."""
    user = db.get_user(user_id)
    if not user or not user.get('email'):
        return None
    tokens = db.get_user_tokens(user_id)
    active = [t for t in tokens if t['active']]
    if not active:
        return None
    token = active[0]   # newest first per get_user_tokens()
    subject = DEFAULT_SUBJECT
    conn = db.get_db()
    cur = conn.execute(
        "INSERT INTO email_jobs (user_id, token_id, to_email, subject) "
        "VALUES (?, ?, ?, ?)",
        (user_id, token['id'], user['email'], subject))
    db.commit()
    job_id = cur.lastrowid
    email_bus.publish({'type': 'email-job', 'id': job_id, 'user_id': user_id,
                        'status': 'queued'})
    return job_id


def _process_job(job: dict) -> None:
    """Mark sending, send via SMTP, mark sent/failed. Publishes state
    transitions on the email_bus."""
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

    # Render the QR PNG fresh from the token (we trust the qr_utils helper).
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

    # Build the per-user template context. unit_label / building come
    # from the joined external_users row when available; manual users
    # get blanks. {qr_code} -> inline <img>; HTML template that omits
    # the placeholder still works because we append the image below.
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
    # Plain-text body: substitute qr_code with a friendly note instead
    # of raw HTML, since cid:qrcode means nothing in plain text.
    text_ctx = {**ctx, 'qr_code': '[QR code attached as image]'}
    body_text = _render_template(body_text_tmpl, text_ctx)
    # If the HTML body doesn't reference the QR placeholder, append the
    # image so the recipient always gets it.
    if 'cid:qrcode' not in body_html:
        body_html += f'<p style="text-align:center;margin:1.5em 0;">{QR_IMG_TAG}</p>'

    try:
        send_one(cfg, user['email'], subject, body_html, body_text, qr_bytes)
    except Exception as e:
        _mark_failed(job_id, user_id, str(e)[:500])
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
    conn = db.get_db()
    row = conn.execute(
        "SELECT id, user_id, token_id, to_email "
        "FROM email_jobs WHERE status='queued' ORDER BY id LIMIT 1"
    ).fetchone()
    return dict(row) if row else None
