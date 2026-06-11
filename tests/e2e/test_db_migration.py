"""Migration regression: legacy users table with NOT NULL UNIQUE email
must get rebuilt to nullable + non-unique on init_db().

Production data has the original schema where emails couldn't be
nullable or duplicated. After Buildium sync arrived (which needs both),
init_db() must rebuild the table on existing installs - otherwise sync
hits "UNIQUE constraint failed" on the first spouse-share-email and
rolls the whole batch back."""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path


def _legacy_db_path(tmp_path: Path) -> Path:
    """Create a SQLite file with the original (pre-Buildium) users
    schema and a couple of seed rows."""
    config_dir = tmp_path / 'config'
    config_dir.mkdir()
    path = config_dir / 'qr_access.db'
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            address TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        INSERT INTO users (full_name, email, address)
            VALUES ('Old User', 'old@example.com', '123 Old St');
    """)
    conn.commit()
    conn.close()
    return config_dir


def test_legacy_email_unique_constraint_dropped_on_init(tmp_path, monkeypatch):
    config_dir = _legacy_db_path(tmp_path)
    monkeypatch.setenv('QR_CONFIG_DIR', str(config_dir))
    monkeypatch.setenv('QR_ADMIN_PASSWORD', '')
    # Force module re-import so DB_PATH picks up the new env.
    for name in [n for n in list(sys.modules) if n == 'web' or n.startswith('web.')]:
        del sys.modules[name]

    from web import db
    db.init_db()

    conn = db.get_db()
    # email column must now be nullable
    cols = conn.execute("PRAGMA table_info(users)").fetchall()
    email_col = next(c for c in cols if c['name'] == 'email')
    assert email_col['notnull'] == 0, 'email should be nullable after migration'

    # Insert two users sharing an email - must succeed
    db.create_user('Spouse A', 'shared@example.com', 'Unit 1')
    db.create_user('Spouse B', 'shared@example.com', 'Unit 1')
    rows = conn.execute(
        "SELECT COUNT(*) AS n FROM users WHERE email='shared@example.com'"
    ).fetchone()
    assert rows['n'] == 2

    # And the seed row from the legacy schema survived the rebuild.
    seed = conn.execute(
        "SELECT full_name FROM users WHERE id=1"
    ).fetchone()
    assert seed['full_name'] == 'Old User'

    # Also: a NULL email row is now allowed (no constraint violation).
    db.create_user('No Email', None, 'Unit 9')
    has_null = conn.execute(
        "SELECT COUNT(*) AS n FROM users WHERE email IS NULL"
    ).fetchone()
    assert has_null['n'] == 1


def test_relay_ms_column_added_on_init(tmp_path, monkeypatch):
    """access_log gains relay_ms on existing installs, and log_access
    round-trips it (NULL for rows logged without a relay command)."""
    config_dir = tmp_path / 'config'
    config_dir.mkdir()
    conn = sqlite3.connect(config_dir / 'qr_access.db')
    # Pre-relay_ms access_log, as shipped before this migration.
    conn.executescript("""
        CREATE TABLE access_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            door_name TEXT NOT NULL DEFAULT '',
            token TEXT NOT NULL,
            event TEXT NOT NULL,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            video_path TEXT DEFAULT NULL,
            thumbnail_path TEXT DEFAULT NULL,
            decode_ms INTEGER DEFAULT NULL
        );
        INSERT INTO access_log (door_name, token, event, decode_ms)
            VALUES ('Front', 'tok-legacy', 'ACCESS-GRANTED', 766);
    """)
    conn.commit()
    conn.close()

    monkeypatch.setenv('QR_CONFIG_DIR', str(config_dir))
    monkeypatch.setenv('QR_ADMIN_PASSWORD', '')
    for name in [n for n in list(sys.modules) if n == 'web' or n.startswith('web.')]:
        del sys.modules[name]

    from web import db
    db.init_db()

    conn = db.get_db()
    cols = [r['name'] for r in
            conn.execute("PRAGMA table_info(access_log)").fetchall()]
    assert 'relay_ms' in cols

    # Legacy row survived with relay_ms NULL.
    legacy = db.get_access_logs(limit=10)[0]
    assert legacy['token'] == 'tok-legacy'
    assert legacy['relay_ms'] is None

    # New rows round-trip both metrics; relay_ms stays NULL when the
    # relay command wasn't sent (denied / dry-run / unreachable).
    db.log_access('Front', 'tok-new', 'ACCESS-GRANTED',
                  decode_ms=210, relay_ms=140)
    db.log_access('Front', 'tok-denied', 'ACCESS-DENIED', decode_ms=180)
    rows = {r['token']: r for r in db.get_access_logs(limit=10)}
    assert rows['tok-new']['relay_ms'] == 140
    assert rows['tok-new']['decode_ms'] == 210
    assert rows['tok-denied']['relay_ms'] is None
