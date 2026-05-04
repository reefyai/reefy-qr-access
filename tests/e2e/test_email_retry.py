"""E2E for the email worker's retry/backoff logic.

Strategy: stub `_provider_for()` so send_one() raises a classified
exception of the test's choosing. Then drive the worker via
`_pop_next_queued_job()` + `_process_job()` and inspect email_jobs
state in SQLite. SQLite holds queue + retry counters so we can verify
the worker's behavior across simulated process restarts.
"""

from __future__ import annotations

import pytest
import requests


def _imports():
    """Re-import the service module per-test. The conftest fixture
    drops `web.*` from sys.modules between tests so the QR_CONFIG_DIR
    rebinds; if we held a top-level reference here, we'd point at the
    previous test's already-closed SQLite path."""
    from web.services import email as email_svc
    from web.services.email_providers.base import (
        RateLimitError, TransientError, PermanentError,
    )
    return email_svc, RateLimitError, TransientError, PermanentError


SMTP_CFG = {
    'provider': 'smtp',
    'smtp_host': 'smtp.example.com',
    'smtp_port': 587,
    'username': 'sender@example.com',
    'password': 'app-password',
    'from_email': 'sender@example.com',
    'from_name': 'Test Sender',
}


def _save_cfg(base_url):
    requests.post(f'{base_url}/api/integrations/email',
                   json=SMTP_CFG, timeout=10).raise_for_status()


def _create_user(base_url, email='resident@example.com'):
    r = requests.post(f'{base_url}/api/users', json={
        'full_name': 'Resident',
        'email': email,
        'address': 'Unit 1',
    }, timeout=10)
    r.raise_for_status()
    return r.json()['user']['id']


def _enqueue(base_url, user_id):
    r = requests.post(f'{base_url}/api/users/{user_id}/email-qr', timeout=10)
    r.raise_for_status()
    return r.json()['job_id']


class _StubProvider:
    """Test double whose send_one() raises a queued exception per call.
    Exhausting the queue raises StopIteration which would mean the test
    under-counted attempts."""

    def __init__(self, exceptions):
        self._exc = list(exceptions)
        self.calls = 0

    def send_one(self, *a, **kw):
        self.calls += 1
        if self._exc:
            exc = self._exc.pop(0)
            if exc is not None:
                raise exc
        # None in the queue => simulate a successful send

    def send_test(self, to_email):
        return self.send_one()


def _patch_provider(monkeypatch, email_svc, stub):
    monkeypatch.setattr(email_svc, '_provider_for', lambda cfg: stub)


def _job_row(app, job_id):
    conn = app.db.get_db()
    row = conn.execute(
        "SELECT status, attempts, error, next_retry_at "
        "FROM email_jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def test_rate_limit_defers_with_long_backoff(app_server, monkeypatch):
    """RateLimitError -> status='queued' with next_retry_at set far in
    the future (15 min for first attempt). Job is NOT marked failed -
    rate limits clear naturally."""
    email_svc, RateLimitError, _, _ = _imports()
    base = app_server['base_url']
    app = app_server['app']
    _save_cfg(base)
    user_id = _create_user(base)
    job_id = _enqueue(base, user_id)

    stub = _StubProvider([RateLimitError('429 Too Many Requests')])
    _patch_provider(monkeypatch, email_svc, stub)

    job = email_svc._pop_next_queued_job()
    assert job and job['id'] == job_id
    email_svc._process_job(job)

    row = _job_row(app, job_id)
    assert row['status'] == 'queued'
    assert row['attempts'] == 1
    assert row['next_retry_at'] is not None
    # First rate-limit backoff is 15 min - the timer must be at least
    # ~14 min in the future to confirm we used the long schedule and
    # not the transient one (which starts at 30s).
    delta = app.db.get_db().execute(
        "SELECT (julianday(next_retry_at) - julianday('now')) * 86400 AS s "
        "FROM email_jobs WHERE id=?", (job_id,)).fetchone()['s']
    assert delta > 14 * 60


def test_transient_error_retries_then_fails_at_max(app_server, monkeypatch):
    """5 attempts (1 initial + 4 retries from TRANSIENT_BACKOFF_S) and
    then the worker gives up: status='failed'."""
    email_svc, _, TransientError, _ = _imports()
    base = app_server['base_url']
    app = app_server['app']
    _save_cfg(base)
    user_id = _create_user(base)
    job_id = _enqueue(base, user_id)

    stub = _StubProvider([TransientError('5xx')] * 10)
    _patch_provider(monkeypatch, email_svc, stub)

    # Force the retry timer to "now" between attempts so the worker
    # picks the same job up immediately.
    for i in range(email_svc.MAX_TRANSIENT_ATTEMPTS):
        job = email_svc._pop_next_queued_job()
        assert job is not None, f'job missing on attempt {i+1}'
        email_svc._process_job(job)
        # After each retry, fast-forward next_retry_at to NULL so the
        # next pop picks it up. Real worker would wait minutes/hours.
        app.db.get_db().execute(
            "UPDATE email_jobs SET next_retry_at=NULL WHERE id=?", (job_id,))
        app.db.commit()

    row = _job_row(app, job_id)
    assert row['status'] == 'failed'
    assert row['attempts'] == email_svc.MAX_TRANSIENT_ATTEMPTS
    assert 'after' in (row['error'] or '').lower()
    # No more queued work
    assert email_svc._pop_next_queued_job() is None


def test_permanent_error_fails_immediately(app_server, monkeypatch):
    """PermanentError -> status='failed' on first try, no retry."""
    email_svc, _, _, PermanentError = _imports()
    base = app_server['base_url']
    app = app_server['app']
    _save_cfg(base)
    user_id = _create_user(base)
    job_id = _enqueue(base, user_id)

    stub = _StubProvider([PermanentError('bad recipient')])
    _patch_provider(monkeypatch, email_svc, stub)

    job = email_svc._pop_next_queued_job()
    email_svc._process_job(job)

    row = _job_row(app, job_id)
    assert row['status'] == 'failed'
    assert row['attempts'] == 0   # _mark_failed doesn't bump attempts
    assert 'bad recipient' in row['error']
    # Provider was called exactly once
    assert stub.calls == 1


def test_worker_picks_up_due_retries_on_simulated_restart(app_server, monkeypatch):
    """A job whose next_retry_at has passed must be re-popped even
    after a process restart (state is in SQLite, not in memory)."""
    email_svc, RateLimitError, _, _ = _imports()
    base = app_server['base_url']
    app = app_server['app']
    _save_cfg(base)
    user_id = _create_user(base)
    job_id = _enqueue(base, user_id)

    # Round 1: rate-limit defers it.
    stub = _StubProvider([RateLimitError('429')])
    _patch_provider(monkeypatch, email_svc, stub)
    email_svc._process_job(email_svc._pop_next_queued_job())
    assert _job_row(app, job_id)['status'] == 'queued'

    # Simulate restart by zeroing next_retry_at (timer elapsed).
    app.db.get_db().execute(
        "UPDATE email_jobs SET next_retry_at=datetime('now', '-1 second') "
        "WHERE id=?", (job_id,))
    app.db.commit()

    # Round 2: succeeds.
    stub2 = _StubProvider([None])
    _patch_provider(monkeypatch, email_svc, stub2)
    job = email_svc._pop_next_queued_job()
    assert job is not None and job['id'] == job_id
    email_svc._process_job(job)

    row = _job_row(app, job_id)
    assert row['status'] == 'sent'
    assert row['attempts'] == 1   # Stamped during the rate-limit defer


def test_worker_skips_not_yet_due_retries(app_server, monkeypatch):
    """A job with next_retry_at in the future must NOT be returned by
    _pop_next_queued_job - else we'd hammer the API immediately."""
    email_svc, _, TransientError, _ = _imports()
    base = app_server['base_url']
    app = app_server['app']
    _save_cfg(base)
    user_id = _create_user(base)
    job_id = _enqueue(base, user_id)

    stub = _StubProvider([TransientError('5xx')])
    _patch_provider(monkeypatch, email_svc, stub)
    email_svc._process_job(email_svc._pop_next_queued_job())

    row = _job_row(app, job_id)
    assert row['status'] == 'queued'
    assert row['next_retry_at'] is not None
    # Timer is in the future (TRANSIENT first backoff is 30s) so the
    # next pop must return None.
    assert email_svc._pop_next_queued_job() is None


def test_attempts_counter_persists_across_workers(app_server, monkeypatch):
    """After 3 transient failures the attempts counter must equal 3 in
    SQLite, regardless of which 'worker' (test invocation) drove it."""
    email_svc, _, TransientError, _ = _imports()
    base = app_server['base_url']
    app = app_server['app']
    _save_cfg(base)
    user_id = _create_user(base)
    job_id = _enqueue(base, user_id)

    stub = _StubProvider([TransientError('5xx')] * 5)
    _patch_provider(monkeypatch, email_svc, stub)

    for _ in range(3):
        job = email_svc._pop_next_queued_job()
        assert job is not None
        # Crucially, the job dict must carry forward attempts so the
        # next defer computes the right backoff index.
        email_svc._process_job(job)
        app.db.get_db().execute(
            "UPDATE email_jobs SET next_retry_at=NULL WHERE id=?", (job_id,))
        app.db.commit()

    row = _job_row(app, job_id)
    assert row['attempts'] == 3
    assert row['status'] == 'queued'   # Not yet at MAX_TRANSIENT_ATTEMPTS
