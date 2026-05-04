#!/usr/bin/env python3
"""Flask web interface for QR Access control system."""

import os
import time
import json
import functools
from flask import (Flask, render_template, request, redirect, url_for,
                   session, jsonify, send_file, flash, Response, stream_with_context)

from . import db
from .db import get_db
from .qr_utils import create_token, generate_qr_png, get_qr_path
from .config_export import export_config
from .events import event_bus
# .discovery is imported lazily inside route handlers because it pulls in
# cv2 + ONVIF stack, which we don't want loaded by lightweight callers (tests,
# CLI helpers) that never hit a scan endpoint.

app = Flask(__name__,
            template_folder='templates',
            static_folder='static')

app.secret_key = os.environ.get('QR_SECRET_KEY') \
                  or os.environ.get('QR_ADMIN_PASSWORD') \
                  or 'qr-access-default-secret'

ADMIN_PASSWORD = os.environ.get('QR_ADMIN_PASSWORD', '')


# --- Auth ---

def login_required(f):
    @functools.wraps(f)
    def wrapped(*args, **kwargs):
        if not ADMIN_PASSWORD:
            session['authenticated'] = True
        elif not session.get('authenticated'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapped


@app.route('/login', methods=['GET', 'POST'])
def login():
    if not ADMIN_PASSWORD:
        session['authenticated'] = True
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        if request.form.get('password') == ADMIN_PASSWORD:
            session['authenticated'] = True
            return redirect(url_for('dashboard'))
        flash('Invalid password', 'error')

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# --- Pages ---

@app.route('/')
@login_required
def dashboard():
    users = db.get_users()
    return render_template('dashboard.html', users=users)


LOGS_PAGE_SIZE = 50


@app.route('/logs')
@login_required
def logs():
    entries = db.get_access_logs(limit=LOGS_PAGE_SIZE, offset=0)
    total = db.count_access_logs()
    return render_template('logs.html', entries=entries,
                           total=total, page_size=LOGS_PAGE_SIZE)


@app.route('/api/access-logs')
@login_required
def api_access_logs():
    """Paginated access log feed for the Logs page's pager. Default
    offset=0 (page 1 = newest); limit clamped to 200 to bound DB +
    transfer cost on misconfigured clients."""
    try:
        offset = max(int(request.args.get('offset', 0)), 0)
        limit = min(max(int(request.args.get('limit', LOGS_PAGE_SIZE)), 1), 200)
    except (ValueError, TypeError):
        return jsonify(error='invalid offset/limit'), 400
    return jsonify(
        entries=db.get_access_logs(limit=limit, offset=offset),
        total=db.count_access_logs(),
        offset=offset,
        limit=limit,
    )


@app.route('/api/logs/stream')
@login_required
def logs_stream():
    """Server-Sent Events stream using in-memory event bus (no polling)."""
    def generate():
        q = event_bus.subscribe()
        try:
            while True:
                try:
                    msg = q.get(timeout=15)
                    yield f"data: {msg}\n\n"
                except Exception:
                    # Keepalive on timeout
                    yield ": keepalive\n\n"
        finally:
            event_bus.unsubscribe(q)

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        }
    )


@app.route('/api/video/<path:video_path>')
@login_required
def api_video(video_path):
    """Serve recorded event videos."""
    video_dir = os.path.join(os.getcwd(), 'video-logs')
    full_path = os.path.realpath(os.path.join(video_dir, video_path))
    # Prevent path traversal
    if not full_path.startswith(os.path.realpath(video_dir)):
        return 'Forbidden', 403
    if not os.path.exists(full_path):
        return 'Not found', 404
    return send_file(full_path, mimetype='video/mp4')


@app.route('/api/thumbnail/<path:thumb_path>')
@login_required
def api_thumbnail(thumb_path):
    """Serve event thumbnails saved alongside the video."""
    video_dir = os.path.join(os.getcwd(), 'video-logs')
    full_path = os.path.realpath(os.path.join(video_dir, thumb_path))
    if not full_path.startswith(os.path.realpath(video_dir)):
        return 'Forbidden', 403
    if not os.path.exists(full_path):
        return 'Not found', 404
    return send_file(full_path, mimetype='image/jpeg')


@app.route('/settings')
@login_required
def settings():
    from .version import get_version
    doors = db.get_doors()
    cameras = db.get_cameras()
    shellys = db.get_shellys()
    syslog_lines = _log_capture.get_lines(500) if _log_capture else []
    return render_template('settings.html', doors=doors,
                           cameras=cameras, shellys=shellys,
                           syslog_lines=syslog_lines,
                           version=get_version())


# --- User API ---

@app.route('/api/users', methods=['POST'])
@login_required
def api_create_user():
    data = request.get_json()
    full_name = data.get('full_name', '').strip()
    email = data.get('email', '').strip()
    phone = data.get('phone', '').strip() or None
    address = data.get('address', '').strip()

    if not full_name or not email:
        return jsonify(error='Name and email are required'), 400

    try:
        user_id = db.create_user(full_name, email, address, phone=phone)
    except Exception as e:
        err = str(e)
        if 'UNIQUE' in err and 'email' in err:
            return jsonify(error='A user with this email already exists.'), 400
        if 'locked' in err.lower():
            return jsonify(error='Database busy, please try again.'), 503
        return jsonify(error=err), 400

    from .services.users import issue_initial_token
    issue_initial_token(user_id)

    _try_export_config()
    return jsonify(user=db.get_user(user_id)), 201


@app.route('/api/users/<int:user_id>', methods=['DELETE'])
@login_required
def api_delete_user(user_id):
    db.delete_user(user_id)
    _try_export_config()
    return jsonify(ok=True)


@app.route('/api/users/<int:user_id>/tokens')
@login_required
def api_user_tokens(user_id):
    tokens = db.get_user_tokens(user_id)
    return jsonify(tokens=tokens)


@app.route('/api/users/<int:user_id>/tokens', methods=['POST'])
@login_required
def api_add_token(user_id):
    data = request.get_json()
    comment = data.get('comment', '').strip() or 'added'

    token = create_token()
    db.create_token_for_user(user_id, token, comment=comment)
    generate_qr_png(token)

    _try_export_config()
    return jsonify(token=token, comment=comment), 201


@app.route('/api/tokens/<int:token_id>/revoke', methods=['POST'])
@login_required
def api_revoke_token(token_id):
    db.revoke_token(token_id)
    _try_export_config()
    return jsonify(ok=True)


@app.route('/api/tokens/<int:token_id>/qr')
@login_required
def api_token_qr(token_id):
    """Serve QR code PNG for a specific token."""
    d = get_db()
    row = d.execute("SELECT token FROM tokens WHERE id=?",
                    (token_id,)).fetchone()
    d.close()
    if not row:
        return 'Not found', 404

    token_val = row['token']
    path = get_qr_path(token_val)
    if not path.exists():
        generate_qr_png(token_val)

    return send_file(str(path.resolve()), mimetype='image/png')


@app.route('/api/tokens/<int:token_id>/qr-labeled')
@login_required
def api_token_qr_labeled(token_id):
    """Serve QR code PNG with user name and date label."""
    from io import BytesIO
    from PIL import Image, ImageDraw, ImageFont

    d = get_db()
    row = d.execute("""
        SELECT tokens.token, tokens.created_at, users.full_name
        FROM tokens JOIN users ON tokens.user_id = users.id
        WHERE tokens.id = ?
    """, (token_id,)).fetchone()
    d.close()
    if not row:
        return 'Not found', 404

    # Get or generate QR image
    qr_path = get_qr_path(row['token'])
    if not qr_path.exists():
        generate_qr_png(row['token'])

    name = request.args.get('name', row['full_name'])
    comment = request.args.get('comment', '')
    date = row['created_at'][:10]

    # Build labeled image
    qr_img = Image.open(qr_path).convert('RGB')
    qr_size = max(qr_img.size[0], 300)
    qr_img = qr_img.resize((qr_size, qr_size), Image.NEAREST)

    padding = 30
    text_h = 90 if comment else 70
    width = qr_size + padding * 2
    height = qr_size + padding * 2 + text_h

    canvas = Image.new('RGB', (width, height), 'white')
    canvas.paste(qr_img, (padding, padding))

    draw = ImageDraw.Draw(canvas)
    try:
        font_name = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
        font_date = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
    except OSError:
        font_name = ImageFont.load_default()
        font_date = font_name

    text_y = padding + qr_size + 10
    draw.text((padding, text_y), name, fill='black', font=font_name)
    meta = f"Created: {date}"
    if comment:
        meta += f"  •  {comment}"
    draw.text((padding, text_y + 28), meta, fill='gray', font=font_date)

    buf = BytesIO()
    canvas.save(buf, format='PNG')
    buf.seek(0)
    return send_file(buf, mimetype='image/png',
                     download_name=f"qr-{name.replace(' ', '_')}.png")


# --- Network scan API ---

@app.route('/api/scan-network', methods=['POST'])
@login_required
def api_scan_network():
    from .discovery import scan_cameras, scan_shellys
    cameras = scan_cameras(timeout=3)
    shellys = scan_shellys(timeout=5)

    db.upsert_cameras(cameras)
    db.upsert_shellys(shellys)

    return jsonify(
        cameras=[c for c in cameras],
        shellys=shellys,
    )


@app.route('/api/camera-rtsp', methods=['POST'])
@login_required
def api_camera_rtsp():
    """Fetch RTSP stream URLs from a camera using ONVIF with credentials."""
    from .discovery import fetch_camera_rtsp
    data = request.get_json() or {}
    ip = data.get('ip', '').strip()
    username = data.get('username', '').strip()
    password = data.get('password', '')
    xaddr = data.get('xaddr', '')

    if not ip:
        return jsonify(error='Camera IP is required'), 400

    urls = fetch_camera_rtsp(ip, username=username, password=password, xaddr=xaddr)

    # Update camera in DB with fetched RTSP URLs
    if urls:
        import json
        cam_db = get_db()
        cam_db.execute(
            "UPDATE cameras SET rtsp_urls = ? WHERE ip = ?",
            (json.dumps(urls), ip))
        cam_db.commit()

    return jsonify(rtsp_urls=urls)


# --- Door API ---

@app.route('/api/doors', methods=['POST'])
@login_required
def api_create_door():
    data = request.get_json()
    name = data.get('name', '').strip()
    if not name:
        return jsonify(error='Door name is required'), 400

    door_id = db.create_door(
        name=name,
        camera_uuid=data.get('camera_uuid', ''),
        camera_user=data.get('camera_user', 'admin'),
        camera_pass=data.get('camera_pass', ''),
        camera_path=data.get('camera_path', '/stream2'),
        camera_port=int(data.get('camera_port', 554)),
        shelly_device_id=data.get('shelly_device_id', ''),
        shelly_pass=data.get('shelly_pass', ''),
        open_seconds=int(data.get('open_seconds', 5)),
    )

    _try_export_config()
    return jsonify(door=db.get_door(door_id)), 201


@app.route('/api/doors/<int:door_id>', methods=['PUT'])
@login_required
def api_update_door(door_id):
    data = request.get_json()
    allowed = ['name', 'camera_uuid', 'camera_user', 'camera_pass',
               'camera_path', 'camera_port', 'shelly_device_id',
               'shelly_pass', 'open_seconds']
    updates = {k: data[k] for k in allowed if k in data}
    if 'camera_port' in updates:
        updates['camera_port'] = int(updates['camera_port'])
    if 'open_seconds' in updates:
        updates['open_seconds'] = int(updates['open_seconds'])

    db.update_door(door_id, **updates)
    _try_export_config()
    return jsonify(door=db.get_door(door_id))


@app.route('/api/doors/<int:door_id>', methods=['DELETE'])
@login_required
def api_delete_door(door_id):
    db.delete_door(door_id)
    _try_export_config()
    return jsonify(ok=True)


# --- Shelly test ---

@app.route('/api/test-shelly', methods=['POST'])
@login_required
def api_test_shelly():
    """Test Shelly relay access by querying its status."""
    data = request.get_json() or {}
    door_id = data.get('door_id')
    if not door_id:
        return jsonify(error='door_id required'), 400

    door = db.get_door(door_id)
    if not door or not door['shelly_device_id']:
        return jsonify(error='Door or Shelly not configured'), 400

    # Resolve Shelly IP
    shelly_id = door['shelly_device_id']
    shelly_ip = None
    shellys = db.get_shellys()
    for s in shellys:
        if s['device_id'] == shelly_id:
            shelly_ip = s['ip']
            break

    if not shelly_ip:
        return jsonify(error=f'Shelly {shelly_id} IP not found. Run Scan Network first.'), 400

    import requests as req
    from requests.auth import HTTPDigestAuth
    try:
        auth = HTTPDigestAuth("admin", door['shelly_pass']) if door['shelly_pass'] else None
        resp = req.get(f'http://{shelly_ip}/rpc/Switch.GetStatus?id=0',
                       auth=auth, timeout=5)
        resp.raise_for_status()
        status = resp.json()
        return jsonify(ok=True, status=status, ip=shelly_ip)
    except Exception as e:
        return jsonify(error=str(e)), 500


# --- Camera status ---

@app.route('/api/camera-status', methods=['GET'])
@login_required
def api_camera_status():
    """Get detector status for all doors (last frame time, FPS)."""
    try:
        status_path = 'config/detector_status.json'
        if os.path.exists(status_path):
            with open(status_path) as f:
                return jsonify(json.load(f))
    except Exception:
        pass
    return jsonify({})


# --- System logs ---

@app.route('/system-logs')
@login_required
def system_logs():
    lines = []
    if _log_capture:
        lines = _log_capture.get_lines(500)
    return render_template('system_logs.html', lines=lines)


@app.route('/api/system-logs/stream')
@login_required
def system_logs_stream():
    """SSE stream of all stdout/stderr output."""
    if not _log_capture:
        return jsonify(error='Log capture not available'), 500

    def generate():
        q = _log_capture.subscribe()
        eq = _err_capture.subscribe() if _err_capture else None
        try:
            while True:
                # Check stdout
                try:
                    line = q.get(timeout=1)
                    yield f"data: {json.dumps({'line': line, 'stream': 'stdout'})}\n\n"
                except Exception:
                    pass
                # Check stderr
                if eq:
                    try:
                        line = eq.get_nowait()
                        yield f"data: {json.dumps({'line': line, 'stream': 'stderr'})}\n\n"
                    except Exception:
                        pass
        finally:
            _log_capture.unsubscribe(q)
            if eq:
                _err_capture.unsubscribe(eq)

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )


# --- Version ---

from .version import get_version


@app.route('/api/version')
@login_required
def api_version():
    return jsonify(version=get_version())


# --- Integrations: Buildium ---

DEFAULT_BUILDIUM_BASE_URL = 'https://api.buildium.com/v1'


def _buildium_status_payload():
    cfg = db.get_setting('buildium.config') or {}
    last_sync = db.get_setting('buildium.last_sync')
    cid = cfg.get('client_id', '')
    return {
        'configured': bool(cfg.get('client_id') and cfg.get('client_secret')),
        'base_url': cfg.get('base_url', DEFAULT_BUILDIUM_BASE_URL),
        'client_id_tail': cid[-4:] if len(cid) >= 4 else '',
        'last_sync': last_sync,
    }


@app.route('/api/integrations/buildium', methods=['GET'])
@login_required
def api_buildium_status():
    return jsonify(_buildium_status_payload())


@app.route('/api/integrations/buildium', methods=['POST'])
@login_required
def api_buildium_save():
    from .services.buildium import BuildiumClient, BuildiumError
    data = request.get_json() or {}
    saved = db.get_setting('buildium.config') or {}
    client_id = (data.get('client_id') or '').strip()
    # Empty/omitted secret means "keep the saved one" - lets the admin
    # re-test or change Client ID without re-pasting the secret.
    client_secret = (data.get('client_secret') or '').strip() or saved.get('client_secret', '')
    base_url = (data.get('base_url') or '').strip() or DEFAULT_BUILDIUM_BASE_URL

    if not client_id or not client_secret:
        return jsonify(error='client_id and client_secret are required'), 400

    # Validate before saving so we never store bogus credentials.
    try:
        BuildiumClient(client_id, client_secret, base_url).test_connection()
    except BuildiumError as e:
        return jsonify(error=str(e)), 400
    except Exception as e:
        return jsonify(error=f'Connection failed: {e}'), 500

    db.set_setting('buildium.config', {
        'client_id': client_id,
        'client_secret': client_secret,
        'base_url': base_url,
    })
    return jsonify(ok=True, **_buildium_status_payload())


@app.route('/api/integrations/buildium/test', methods=['POST'])
@login_required
def api_buildium_test():
    """Validate creds without persisting. Falls back to saved config if body
    omits any field, so the UI's 'Test' button works after Save too."""
    from .services.buildium import BuildiumClient, BuildiumError
    data = request.get_json() or {}
    saved = db.get_setting('buildium.config') or {}
    client_id = (data.get('client_id') or saved.get('client_id') or '').strip()
    client_secret = (data.get('client_secret') or saved.get('client_secret') or '').strip()
    base_url = (data.get('base_url') or saved.get('base_url')
                or DEFAULT_BUILDIUM_BASE_URL).strip()

    if not client_id or not client_secret:
        return jsonify(error='client_id and client_secret are required'), 400
    try:
        result = BuildiumClient(client_id, client_secret, base_url).test_connection()
    except BuildiumError as e:
        return jsonify(error=str(e)), 400
    except Exception as e:
        return jsonify(error=f'Connection failed: {e}'), 500
    # result already carries ok=True from BuildiumClient.test_connection()
    return jsonify(**result)


@app.route('/api/integrations/buildium/sync', methods=['POST'])
@login_required
def api_buildium_sync():
    from .services.buildium import BuildiumClient, run_sync
    cfg = db.get_setting('buildium.config') or {}
    if not cfg.get('client_id') or not cfg.get('client_secret'):
        return jsonify(error='Buildium not configured'), 400

    client = BuildiumClient(
        cfg['client_id'],
        cfg['client_secret'],
        cfg.get('base_url') or DEFAULT_BUILDIUM_BASE_URL,
    )
    result = run_sync(client)
    db.set_setting('buildium.last_sync', {
        'at': db._now(),
        'counts': result,
    })

    _try_export_config()
    return jsonify(result)


# --- Integrations: Email ---


@app.route('/api/integrations/email', methods=['GET'])
@login_required
def api_email_status():
    from .services.email import public_status
    return jsonify(public_status())


@app.route('/api/integrations/email', methods=['DELETE'])
@login_required
def api_email_disconnect():
    """Wipe saved provider config so admin can replace it cleanly.
    Keeps templates so they survive a reconnect."""
    saved = db.get_setting('email.config') or {}
    db.set_setting('email.config', {
        'subject_template': saved.get('subject_template', ''),
        'body_html_template': saved.get('body_html_template', ''),
        'body_text_template': saved.get('body_text_template', ''),
    })
    return jsonify(ok=True)


@app.route('/api/integrations/email', methods=['POST'])
@login_required
def api_email_save():
    """Save provider config. Empty password / api_key keeps the saved
    value so the admin can tweak other fields without re-pasting the
    secret. Validates provider-specific required fields before saving."""
    data = request.get_json() or {}
    saved = db.get_setting('email.config') or {}
    provider = (data.get('provider') or saved.get('provider') or 'smtp').lower()

    # Common fields (templates + From identity) survive across providers.
    cfg = {
        'provider': provider,
        'from_email': (data.get('from_email') or '').strip(),
        'from_name': (data.get('from_name') or '').strip(),
        'reply_to': (data.get('reply_to') or '').strip(),
        'subject_template': data.get('subject_template') or '',
        'body_html_template': data.get('body_html_template') or '',
        'body_text_template': data.get('body_text_template') or '',
    }

    if provider == 'smtp':
        cfg.update({
            'smtp_host': (data.get('smtp_host') or '').strip(),
            'smtp_port': int(data.get('smtp_port') or 587),
            'username': (data.get('username') or '').strip(),
            'password': (data.get('password') or '').strip()
                         or saved.get('password', ''),
        })
        if not (cfg['smtp_host'] and cfg['username'] and cfg['password']
                and cfg['from_email']):
            return jsonify(
                error='smtp_host, username, password, from_email are required'), 400
    elif provider == 'agentmail':
        cfg.update({
            'agentmail_api_key': (data.get('agentmail_api_key') or '').strip()
                                  or saved.get('agentmail_api_key', ''),
            # Persist any inbox_id we previously auto-provisioned.
            'agentmail_inbox_id': saved.get('agentmail_inbox_id', ''),
        })
        if not cfg['agentmail_api_key']:
            return jsonify(error='agentmail_api_key is required'), 400
    else:
        return jsonify(error=f'unknown provider: {provider!r}'), 400

    db.set_setting('email.config', cfg)
    return jsonify(ok=True)


@app.route('/api/integrations/email/test', methods=['POST'])
@login_required
def api_email_test():
    """Send a probe email via the configured provider. Body fields
    override saved config so the admin can preview without saving;
    omitted fields fall back to whatever's persisted."""
    from .services.email import send_test
    data = request.get_json() or {}
    saved = db.get_setting('email.config') or {}
    provider = (data.get('provider') or saved.get('provider') or 'smtp').lower()
    to_email = (data.get('to_email') or '').strip()
    if not to_email:
        return jsonify(error='to_email is required'), 400

    cfg = {
        'provider': provider,
        'from_email': (data.get('from_email') or saved.get('from_email') or '').strip(),
        'from_name': (data.get('from_name') or saved.get('from_name') or '').strip(),
    }
    if provider == 'smtp':
        cfg.update({
            'smtp_host': (data.get('smtp_host') or saved.get('smtp_host') or '').strip(),
            'smtp_port': int(data.get('smtp_port') or saved.get('smtp_port') or 587),
            'username': (data.get('username') or saved.get('username') or '').strip(),
            'password': (data.get('password') or saved.get('password') or '').strip(),
        })
        if not (cfg['smtp_host'] and cfg['username'] and cfg['password']
                and cfg['from_email']):
            return jsonify(error='SMTP credentials incomplete'), 400
    elif provider == 'agentmail':
        cfg.update({
            'agentmail_api_key': (data.get('agentmail_api_key')
                                    or saved.get('agentmail_api_key') or '').strip(),
            'agentmail_inbox_id': saved.get('agentmail_inbox_id', ''),
        })
        if not cfg['agentmail_api_key']:
            return jsonify(error='agentmail_api_key is required'), 400
    else:
        return jsonify(error=f'unknown provider: {provider!r}'), 400

    try:
        send_test(cfg, to_email)
    except Exception as e:
        return jsonify(error=str(e)[:500]), 400
    return jsonify(ok=True, sent_to=to_email)


@app.route('/api/users/<int:user_id>/email-qr', methods=['POST'])
@login_required
def api_user_email_qr(user_id):
    from .services.email import enqueue_for_user, is_configured
    if not is_configured():
        return jsonify(error='Email integration not configured'), 400
    job_id = enqueue_for_user(user_id)
    if job_id is None:
        return jsonify(error='User has no email or no active token'), 400
    return jsonify(ok=True, job_id=job_id)


@app.route('/api/users/email-qr-batch', methods=['POST'])
@login_required
def api_users_email_qr_batch():
    """Bulk-enqueue: one email_jobs row per user. Returns counts so the
    UI can show {queued, skipped} feedback. Worker drains the queue
    serially with the configured per-send pacing."""
    from .services.email import enqueue_for_user, is_configured
    if not is_configured():
        return jsonify(error='Email integration not configured'), 400
    data = request.get_json() or {}
    ids = data.get('user_ids') or []
    if not isinstance(ids, list) or not ids:
        return jsonify(error='user_ids must be a non-empty list'), 400
    queued, skipped = 0, 0
    for uid in ids:
        try:
            uid_int = int(uid)
        except (ValueError, TypeError):
            skipped += 1
            continue
        if enqueue_for_user(uid_int) is None:
            skipped += 1
        else:
            queued += 1
    return jsonify(ok=True, queued=queued, skipped=skipped)


@app.route('/api/email-jobs/stream')
@login_required
def api_email_jobs_stream():
    """SSE stream of email-job state changes. Dashboard listens here so
    the per-row 'Last sent' cell updates without polling."""
    from .events import email_bus

    def generate():
        q = email_bus.subscribe()
        try:
            while True:
                try:
                    msg = q.get(timeout=15)
                    yield f"data: {msg}\n\n"
                except Exception:
                    yield ': keepalive\n\n'
        finally:
            email_bus.unsubscribe(q)

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


# --- Config export ---

@app.route('/api/export-config', methods=['POST'])
@login_required
def api_export_config():
    config = export_config()
    return jsonify(config=config, message='Config exported to config/doors.yaml')


_reload_event = None
_log_capture = None
_err_capture = None

def set_reload_event(event):
    """Set the reload event reference (called by run.py at startup)."""
    global _reload_event
    _reload_event = event

def set_log_capture(stdout_cap, stderr_cap):
    """Set the log capture references (called by run.py at startup)."""
    global _log_capture, _err_capture
    _log_capture = stdout_cap
    _err_capture = stderr_cap

def _try_export_config():
    """Export config and signal detector to reload."""
    try:
        export_config()
        if _reload_event:
            _reload_event.set()
    except Exception:
        pass


# --- Init ---

_db_initialized = False

@app.before_request
def ensure_db():
    global _db_initialized
    if not _db_initialized:
        db.init_db()
        _db_initialized = True


def main():
    port = int(os.environ.get('QR_WEB_PORT', 8080))
    host = os.environ.get('QR_WEB_HOST', '0.0.0.0')
    debug = os.environ.get('QR_WEB_DEBUG', '0') == '1'

    print(f"[INFO] QR Access Web UI starting on {host}:{port}")
    if ADMIN_PASSWORD:
        print(f"[INFO] Admin auth: enabled")
    else:
        print(f"[INFO] Admin auth: disabled (no QR_ADMIN_PASSWORD set)")

    db.init_db()
    app.run(host=host, port=port, debug=debug)


if __name__ == '__main__':
    main()
