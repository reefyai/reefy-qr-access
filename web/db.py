"""SQLite database layer for QR Access."""

import sqlite3
import threading
import os
from pathlib import Path

DB_DIR = os.environ.get('QR_CONFIG_DIR', 'config')
DB_PATH = os.path.join(DB_DIR, 'qr_access.db')

# Thread-local storage for connections
_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT NOT NULL,
    email TEXT,
    phone TEXT,
    address TEXT NOT NULL DEFAULT '',
    external_user_id INTEGER REFERENCES external_users(id),
    is_active INTEGER NOT NULL DEFAULT 1,
    created_via TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token TEXT NOT NULL UNIQUE,
    -- 7-char Crockford-style base32 alias encoded in the user's QR.
    -- Camera scans the alias (much smaller QR, ~21x21 modules at ECL H
    -- = ~1.5x bigger modules per cm of printed QR) and the detector
    -- looks the full token up by short_id. Nullable so legacy rows
    -- (and pre-migration installs) still work via the full-token path.
    short_id TEXT UNIQUE,
    comment TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS external_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_id TEXT NOT NULL,
    first_name TEXT NOT NULL DEFAULT '',
    last_name TEXT NOT NULL DEFAULT '',
    email TEXT,
    alternate_email TEXT,
    phone_primary TEXT,
    phone_secondary TEXT,
    unit_source_id TEXT,
    unit_label TEXT,
    building TEXT,
    raw_json TEXT NOT NULL DEFAULT '{}',
    is_active_at_source INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_synced_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (source, source_kind, source_id)
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS email_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_id INTEGER NOT NULL REFERENCES tokens(id) ON DELETE CASCADE,
    to_email TEXT NOT NULL,
    subject TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    queued_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_email_jobs_user_id ON email_jobs(user_id);
CREATE INDEX IF NOT EXISTS idx_email_jobs_status  ON email_jobs(status);

CREATE TABLE IF NOT EXISTS cameras (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ip TEXT NOT NULL,
    uuid TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    hardware TEXT NOT NULL DEFAULT '',
    xaddr TEXT NOT NULL DEFAULT '',
    rtsp_urls TEXT NOT NULL DEFAULT '[]',
    last_seen TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS shellys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL UNIQUE,
    ip TEXT NOT NULL,
    last_seen TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS doors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    camera_uuid TEXT NOT NULL DEFAULT '',
    camera_user TEXT NOT NULL DEFAULT 'admin',
    camera_pass TEXT NOT NULL DEFAULT '',
    camera_path TEXT NOT NULL DEFAULT '/stream2',
    camera_port INTEGER NOT NULL DEFAULT 554,
    shelly_device_id TEXT NOT NULL DEFAULT '',
    shelly_pass TEXT NOT NULL DEFAULT '',
    open_seconds INTEGER NOT NULL DEFAULT 5
);

CREATE TABLE IF NOT EXISTS access_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    door_name TEXT NOT NULL DEFAULT '',
    token TEXT NOT NULL,
    event TEXT NOT NULL,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    video_path TEXT DEFAULT NULL,
    thumbnail_path TEXT DEFAULT NULL,
    -- Milliseconds from the capture timestamp of the first YOLO-detected
    -- QR frame in the burst to the wall-clock instant pyzbar decoded
    -- the token. Surfaces user-perceived "wait at the door" latency.
    decode_ms INTEGER DEFAULT NULL
);
"""

MIGRATION = """
-- Migrate old users table that had token column
-- Only runs if the old schema exists
"""


def get_db():
    """Get a thread-local database connection (reused per thread)."""
    db = getattr(_local, 'db', None)
    if db is None:
        Path(DB_DIR).mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(DB_PATH, timeout=30,
                             check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        _local.db = db
    return db


def init_db():
    db = get_db()
    db.executescript(SCHEMA)

    # Migrate: if old users table has 'token' column, move tokens to new table
    cols = [r[1] for r in db.execute("PRAGMA table_info(users)").fetchall()]
    if 'token' in cols:
        # Move existing tokens to tokens table
        rows = db.execute(
            "SELECT id, token FROM users WHERE token IS NOT NULL AND token != ''"
        ).fetchall()
        for row in rows:
            try:
                db.execute(
                    "INSERT OR IGNORE INTO tokens (user_id, token, comment) "
                    "VALUES (?, ?, 'created')",
                    (row['id'], row['token']))
            except Exception:
                pass

        # Remove old columns by recreating users table
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                address TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            INSERT OR IGNORE INTO users_new (id, full_name, email, address, created_at)
                SELECT id, full_name, email, address, created_at FROM users;
            DROP TABLE users;
            ALTER TABLE users_new RENAME TO users;
        """)

    # Migrate cameras table: add rtsp_urls if missing
    cam_cols = [r[1] for r in db.execute("PRAGMA table_info(cameras)").fetchall()]
    if 'rtsp_urls' not in cam_cols:
        db.execute("ALTER TABLE cameras ADD COLUMN rtsp_urls TEXT NOT NULL DEFAULT '[]'")

    # Migrate access_log table: thumbnail_path for event screenshots.
    # Old rows stay NULL so the dashboard renders the legacy "Play" button
    # for them; new rows populated by the detector get a JPG thumbnail.
    log_cols = [r[1] for r in db.execute("PRAGMA table_info(access_log)").fetchall()]
    if 'thumbnail_path' not in log_cols:
        db.execute("ALTER TABLE access_log ADD COLUMN thumbnail_path TEXT DEFAULT NULL")
    if 'decode_ms' not in log_cols:
        db.execute("ALTER TABLE access_log ADD COLUMN decode_ms INTEGER DEFAULT NULL")

    # Migrate tokens table: short_id for QR payload (6-char Crockford
    # base32 alias). Backfill assigns one to every existing row so the
    # new shorter QRs can be regenerated for any user that wants them;
    # legacy printed QRs encoding the full 32-char token keep working
    # because the detector tries the short_id lookup first, then falls
    # back to a full-token lookup.
    tok_cols = [r[1] for r in db.execute("PRAGMA table_info(tokens)").fetchall()]
    if 'short_id' not in tok_cols:
        # SQLite forbids ALTER TABLE ADD COLUMN with a UNIQUE
        # constraint on an existing table. Add the column nullable,
        # backfill every row, then enforce uniqueness via an index.
        # Fresh installs hit the CREATE TABLE path above which keeps
        # the inline UNIQUE; this path is migration-only.
        db.execute("ALTER TABLE tokens ADD COLUMN short_id TEXT")
        for row in db.execute("SELECT id FROM tokens WHERE short_id IS NULL").fetchall():
            db.execute("UPDATE tokens SET short_id=? WHERE id=?",
                       (_mint_unique_short_id(db), row['id']))
    # Idempotent: enforces uniqueness on every boot, harmless for both
    # the fresh-install path (already UNIQUE inline) and the
    # post-migration state. Partial index so NULL rows don't clash.
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_tokens_short_id "
        "ON tokens(short_id) WHERE short_id IS NOT NULL")

    # Migrate email_jobs: attempts + next_retry_at for the worker's
    # backoff/retry path. Rate-limited or transient failures stay
    # status='queued' but with a future next_retry_at.
    if db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='email_jobs'").fetchone():
        ej_cols = [r[1] for r in db.execute("PRAGMA table_info(email_jobs)").fetchall()]
        if 'attempts' not in ej_cols:
            db.execute("ALTER TABLE email_jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
        if 'next_retry_at' not in ej_cols:
            db.execute("ALTER TABLE email_jobs ADD COLUMN next_retry_at TEXT")

    # Migrate users table: add columns introduced for external-source sync.
    # Must run BEFORE the index below, which references external_user_id.
    user_cols = [r[1] for r in db.execute("PRAGMA table_info(users)").fetchall()]
    if 'external_user_id' not in user_cols:
        db.execute("ALTER TABLE users ADD COLUMN external_user_id INTEGER REFERENCES external_users(id)")
    if 'is_active' not in user_cols:
        db.execute("ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
    if 'created_via' not in user_cols:
        db.execute("ALTER TABLE users ADD COLUMN created_via TEXT NOT NULL DEFAULT 'manual'")
    if 'phone' not in user_cols:
        db.execute("ALTER TABLE users ADD COLUMN phone TEXT")
    if 'last_email_sent_at' not in user_cols:
        db.execute("ALTER TABLE users ADD COLUMN last_email_sent_at TEXT")

    # Drop legacy email NOT NULL + UNIQUE constraint. Existing installs
    # have it from the original schema; CREATE TABLE IF NOT EXISTS in
    # SCHEMA was a no-op so we can't change the column shape without
    # rebuilding the table. Detect via PRAGMA: notnull=1 on email means
    # the old constraint is in force, AND/OR a sqlite_autoindex on
    # users(email) confirms UNIQUE. Buildium sync needs nullable +
    # non-unique because residents can share emails (spouses) and some
    # records have no email at all.
    user_cols_info = db.execute("PRAGMA table_info(users)").fetchall()
    email_col = next((c for c in user_cols_info if c[1] == 'email'), None)
    needs_email_rebuild = bool(email_col and email_col[3] == 1)
    if not needs_email_rebuild:
        # Also catch the case where notnull was already loosened by an
        # older migration but the UNIQUE auto-index lingers.
        idx_rows = db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name='users' "
            "AND name LIKE 'sqlite_autoindex%'"
        ).fetchall()
        # The PRIMARY KEY also creates a sqlite_autoindex; presence of
        # >1 such index implies an extra UNIQUE constraint somewhere.
        needs_email_rebuild = len(idx_rows) > 1
    if needs_email_rebuild:
        db.executescript("""
            CREATE TABLE users_rebuild (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                email TEXT,
                phone TEXT,
                address TEXT NOT NULL DEFAULT '',
                external_user_id INTEGER REFERENCES external_users(id),
                is_active INTEGER NOT NULL DEFAULT 1,
                created_via TEXT NOT NULL DEFAULT 'manual',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                last_email_sent_at TEXT
            );
            INSERT INTO users_rebuild
                (id, full_name, email, phone, address, external_user_id,
                 is_active, created_via, created_at, last_email_sent_at)
                SELECT id, full_name, email, phone, address, external_user_id,
                       is_active, created_via, created_at, last_email_sent_at
                FROM users;
            DROP TABLE users;
            ALTER TABLE users_rebuild RENAME TO users;
        """)

    # Index lives outside SCHEMA so it runs after the column-add migration
    # (CREATE TABLE IF NOT EXISTS is a no-op on pre-existing tables, so the
    # column the index needs may not exist yet at SCHEMA-execute time).
    db.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_users_external_user_id
            ON users(external_user_id) WHERE external_user_id IS NOT NULL
    """)

    db.commit()


# --- User helpers ---

def _commit():
    """Commit, rolling back on error to release locks."""
    db = get_db()
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise


def _write(fn):
    """Execute a write operation with automatic rollback on failure."""
    db = get_db()
    try:
        result = fn(db)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def create_user(full_name, email, address, phone=None):
    def op(db):
        db.execute(
            "INSERT INTO users (full_name, email, address, phone) VALUES (?,?,?,?)",
            (full_name, email, address, phone or None))
        return db.execute("SELECT last_insert_rowid()").fetchone()[0]
    return _write(op)


def get_users():
    db = get_db()
    rows = db.execute("""
        SELECT u.id, u.full_name, u.email, u.address, u.is_active,
               u.created_via, u.external_user_id, u.created_at,
               u.last_email_sent_at,
               COALESCE(u.phone, eu.phone_primary) AS phone_primary,
               eu.unit_label, eu.building,
               eu.alternate_email, eu.source AS external_source,
               eu.source_kind AS external_source_kind,
               eu.is_active_at_source
        FROM users u
        LEFT JOIN external_users eu ON eu.id = u.external_user_id
        ORDER BY u.created_at DESC
    """).fetchall()
    return [dict(r) for r in rows]


def get_user(user_id):
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def delete_user(user_id):
    def op(db):
        db.execute("DELETE FROM users WHERE id=?", (user_id,))
    _write(op)


# --- Token helpers ---

# Crockford base32 set: digits + uppercase ASCII minus I/L/O/U so the
# alias stays unambiguous if a human ever has to read or transcribe one.
# All chars are in QR's alphanumeric mode, so 6 chars encodes to 33 bits
# and fits comfortably in QR Version 1 (21x21 modules) at ECL H -
# ~1.6x bigger modules than the full 32-char hex token's QR at the
# same printed size, which is the whole point of this column.
_SHORT_ID_ALPHABET = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'
_SHORT_ID_LEN = 6


def _mint_unique_short_id(db):
    """Generate a short_id that's not already in `tokens`. Caller must
    hold the write lock (passes `db` from inside an `_write` op or
    init_db). UNIQUE constraint also guards against races at insert
    time; this just keeps the common case collision-free."""
    import secrets
    while True:
        candidate = ''.join(
            secrets.choice(_SHORT_ID_ALPHABET) for _ in range(_SHORT_ID_LEN))
        if not db.execute(
                "SELECT 1 FROM tokens WHERE short_id=?",
                (candidate,)).fetchone():
            return candidate


def create_token_for_user(user_id, token, comment=''):
    def op(db):
        short_id = _mint_unique_short_id(db)
        db.execute(
            "INSERT INTO tokens (user_id, token, short_id, comment) "
            "VALUES (?,?,?,?)",
            (user_id, token, short_id, comment))
        return db.execute("SELECT last_insert_rowid()").fetchone()[0]
    return _write(op)


def get_token_by_payload(payload):
    """Look up a token row by either short_id (new QRs) or full token
    (legacy QRs / config-static lists). Returns the full row as a dict
    or None. The detector calls this on every decoded payload."""
    db = get_db()
    row = db.execute(
        "SELECT * FROM tokens WHERE short_id=? OR token=? LIMIT 1",
        (payload, payload)).fetchone()
    return dict(row) if row else None


def get_token_short_id(token):
    db = get_db()
    row = db.execute(
        "SELECT short_id FROM tokens WHERE token=?", (token,)).fetchone()
    return row['short_id'] if row else None


def get_user_tokens(user_id):
    db = get_db()
    rows = db.execute(
        "SELECT * FROM tokens WHERE user_id=? ORDER BY created_at DESC",
        (user_id,)).fetchall()
    return [dict(r) for r in rows]


def revoke_token(token_id):
    def op(db):
        db.execute("UPDATE tokens SET active=0 WHERE id=?", (token_id,))
    _write(op)


def get_all_active_tokens():
    """Return the set of valid scan payloads (both short_ids and full
    tokens) for every active token. The detector's hot path uses this
    as a single in-memory membership check before calling
    get_token_by_payload for the row details, so a scanned QR matches
    whether it encodes the legacy 32-char token or the new short_id."""
    db = get_db()
    rows = db.execute(
        "SELECT token, short_id FROM tokens WHERE active=1").fetchall()
    out = set()
    for r in rows:
        out.add(r['token'])
        if r['short_id']:
            out.add(r['short_id'])
    return out


# --- Camera helpers ---

def upsert_cameras(cameras):
    import json
    def op(db):
        db.execute("DELETE FROM cameras")
        for cam in cameras:
            rtsp_urls = json.dumps(cam.get('rtsp_urls', []))
            db.execute(
                "INSERT INTO cameras (ip, uuid, name, hardware, xaddr, rtsp_urls) "
                "VALUES (?,?,?,?,?,?)",
                (cam['ip'], cam['uuid'], cam['name'],
                 cam['hardware'], cam['xaddr'], rtsp_urls))
    _write(op)


def get_cameras():
    import json
    db = get_db()
    rows = db.execute("SELECT * FROM cameras ORDER BY name").fetchall()
    result = []
    for r in rows:
        d = dict(r)
        try:
            d['rtsp_urls'] = json.loads(d.get('rtsp_urls', '[]'))
        except (json.JSONDecodeError, TypeError):
            d['rtsp_urls'] = []
        result.append(d)
    return result


# --- Shelly helpers ---

def upsert_shellys(shellys):
    def op(db):
        db.execute("DELETE FROM shellys")
        for device_id, ip in shellys.items():
            db.execute(
                "INSERT INTO shellys (device_id, ip) VALUES (?,?)",
                (device_id, ip))
    _write(op)


def get_shellys():
    db = get_db()
    rows = db.execute("SELECT * FROM shellys ORDER BY device_id").fetchall()
    return [dict(r) for r in rows]


# --- Door helpers ---

def create_door(name, camera_uuid, camera_user, camera_pass, camera_path,
                camera_port, shelly_device_id, shelly_pass, open_seconds):
    def op(db):
        db.execute(
            "INSERT INTO doors (name, camera_uuid, camera_user, camera_pass, "
            "camera_path, camera_port, shelly_device_id, shelly_pass, open_seconds) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (name, camera_uuid, camera_user, camera_pass, camera_path,
             camera_port, shelly_device_id, shelly_pass, open_seconds))
        return db.execute("SELECT last_insert_rowid()").fetchone()[0]
    return _write(op)


def get_doors():
    db = get_db()
    rows = db.execute("SELECT * FROM doors ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def get_door(door_id):
    db = get_db()
    row = db.execute("SELECT * FROM doors WHERE id=?", (door_id,)).fetchone()
    return dict(row) if row else None


def update_door(door_id, **kwargs):
    def op(db):
        sets = ', '.join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values()) + [door_id]
        db.execute(f"UPDATE doors SET {sets} WHERE id=?", vals)
    _write(op)


def delete_door(door_id):
    def op(db):
        db.execute("DELETE FROM doors WHERE id=?", (door_id,))
    _write(op)


# --- Access log helpers ---

def log_access(door_name, token, event, video_path=None, thumbnail_path=None,
               decode_ms=None):
    def op(db):
        db.execute(
            "INSERT INTO access_log (door_name, token, event, video_path, "
            "thumbnail_path, decode_ms) VALUES (?,?,?,?,?,?)",
            (door_name, token, event, video_path, thumbnail_path, decode_ms))
        return db.execute("SELECT last_insert_rowid()").fetchone()[0]
    return _write(op)


def get_access_logs(limit=200, offset=0):
    """Get a page of recent access logs joined with user/token info.
    DESC by id (newest first). Default offset=0 = first page."""
    db = get_db()
    rows = db.execute("""
        SELECT
            access_log.id,
            access_log.door_name,
            access_log.token,
            access_log.event,
            access_log.timestamp,
            access_log.video_path,
            access_log.thumbnail_path,
            access_log.decode_ms,
            users.full_name,
            tokens.active as token_active
        FROM access_log
        LEFT JOIN tokens ON access_log.token = tokens.token
        LEFT JOIN users ON tokens.user_id = users.id
        ORDER BY access_log.id DESC
        LIMIT ? OFFSET ?
    """, (limit, offset)).fetchall()
    return [dict(r) for r in rows]


def count_access_logs():
    db = get_db()
    return db.execute("SELECT COUNT(*) AS n FROM access_log").fetchone()['n']


def get_access_logs_since(last_id):
    """Get access logs newer than last_id, joined with user/token info."""
    db = get_db()
    rows = db.execute("""
        SELECT
            access_log.id,
            access_log.door_name,
            access_log.token,
            access_log.event,
            access_log.timestamp,
            access_log.video_path,
            access_log.thumbnail_path,
            access_log.decode_ms,
            users.full_name,
            tokens.active as token_active
        FROM access_log
        LEFT JOIN tokens ON access_log.token = tokens.token
        LEFT JOIN users ON tokens.user_id = users.id
        WHERE access_log.id > ?
        ORDER BY access_log.id ASC
    """, (last_id,)).fetchall()
    return [dict(r) for r in rows]


# --- App settings (key/value JSON store) ---

def get_setting(key, default=None):
    import json as _json
    db = get_db()
    row = db.execute(
        "SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return _json.loads(row['value'])
    except (ValueError, TypeError):
        return default


def set_setting(key, value):
    import json as _json
    def op(db):
        db.execute("""
            INSERT INTO app_settings (key, value, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
                value=excluded.value,
                updated_at=excluded.updated_at
        """, (key, _json.dumps(value)))
    _write(op)


# --- External users (synced from Buildium / future sources) ---

def get_external_users(source=None):
    """Return all external_users rows. Filter by source if provided."""
    db = get_db()
    if source:
        rows = db.execute(
            "SELECT * FROM external_users WHERE source=?", (source,)).fetchall()
    else:
        rows = db.execute("SELECT * FROM external_users").fetchall()
    return [dict(r) for r in rows]


def upsert_external_user(source, source_kind, source_id, fields):
    """Insert or update an external_users row keyed on
    (source, source_kind, source_id). `fields` is a dict of common columns
    plus raw_json. Returns (row_id, created_bool, reactivated_bool).
    """
    import json as _json
    db = get_db()
    existing = db.execute("""
        SELECT id, is_active_at_source FROM external_users
        WHERE source=? AND source_kind=? AND source_id=?
    """, (source, source_kind, str(source_id))).fetchone()

    payload = dict(fields)
    if 'raw_json' in payload and not isinstance(payload['raw_json'], str):
        payload['raw_json'] = _json.dumps(payload['raw_json'])

    if existing:
        reactivated = existing['is_active_at_source'] == 0
        cols = list(payload.keys()) + ['is_active_at_source', 'last_synced_at']
        sets = ', '.join(f"{c}=?" for c in cols)
        vals = list(payload.values()) + [1, _now()]
        vals.append(existing['id'])
        db.execute(
            f"UPDATE external_users SET {sets} WHERE id=?", vals)
        return existing['id'], False, reactivated
    else:
        cols = ['source', 'source_kind', 'source_id'] + list(payload.keys())
        placeholders = ','.join('?' * len(cols))
        vals = [source, source_kind, str(source_id)] + list(payload.values())
        db.execute(
            f"INSERT INTO external_users ({','.join(cols)}) VALUES ({placeholders})",
            vals)
        new_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        return new_id, True, False


def _now():
    db = get_db()
    return db.execute("SELECT datetime('now')").fetchone()[0]


def find_user_by_external(external_user_id):
    db = get_db()
    row = db.execute(
        "SELECT * FROM users WHERE external_user_id=?",
        (external_user_id,)).fetchone()
    return dict(row) if row else None


def create_user_from_external(external_user_id, full_name, email, address,
                               created_via):
    """Create a user row backed by an external_users row. Bypasses the
    UNIQUE-email validation enforced on manual creates."""
    db = get_db()
    db.execute(
        "INSERT INTO users (full_name, email, address, external_user_id, "
        "is_active, created_via) VALUES (?,?,?,?,1,?)",
        (full_name, email, address, external_user_id, created_via))
    return db.execute("SELECT last_insert_rowid()").fetchone()[0]


def set_user_active(user_id, active):
    db = get_db()
    db.execute("UPDATE users SET is_active=? WHERE id=?",
               (1 if active else 0, user_id))


def revoke_user_tokens(user_id):
    """Mark all of a user's tokens inactive. Returns count revoked."""
    db = get_db()
    cur = db.execute(
        "UPDATE tokens SET active=0 WHERE user_id=? AND active=1",
        (user_id,))
    return cur.rowcount


def begin_transaction():
    """Use as a context manager: `with db.begin_transaction():`."""
    return get_db()


def commit():
    get_db().commit()


def rollback():
    get_db().rollback()
