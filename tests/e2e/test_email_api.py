"""API-level e2e for the email-delivery feature.

Worker is in-process (run.py starts it; tests start the Flask app via
the conftest fixture which doesn't run run.py). Tests poke the worker
directly via the service module so we don't have to wait on the poll
loop. SMTP is mocked via the smtp_mock fixture - no real network."""

from __future__ import annotations

import time

import requests


SMTP_CFG = {
    'smtp_host': 'smtp.example.com',
    'smtp_port': 587,
    'username': 'sender@example.com',
    'password': 'app-password',
    'from_email': 'sender@example.com',
    'from_name': 'Test Sender',
}


def _save_smtp(base_url, **overrides):
    payload = {**SMTP_CFG, **overrides}
    r = requests.post(f'{base_url}/api/integrations/email',
                       json=payload, timeout=10)
    r.raise_for_status()


def _create_user(base_url, email='resident@example.com'):
    r = requests.post(f'{base_url}/api/users', json={
        'full_name': 'Resident One',
        'email': email,
        'address': 'Unit 1',
    }, timeout=10)
    r.raise_for_status()
    return r.json()['user']['id']


def test_smtp_config_save_and_status(app_server, smtp_mock):
    base = app_server['base_url']
    # Status: not configured initially
    s = requests.get(f'{base}/api/integrations/email').json()
    assert s['configured'] is False

    _save_smtp(base)
    s = requests.get(f'{base}/api/integrations/email').json()
    assert s['configured'] is True
    assert s['smtp_host'] == 'smtp.example.com'
    assert s['from_email'] == 'sender@example.com'
    # Password never round-trips
    assert 'password' not in s
    assert s['has_password'] is True


def test_smtp_test_endpoint_sends_probe(app_server, smtp_mock):
    base = app_server['base_url']
    _save_smtp(base)
    r = requests.post(f'{base}/api/integrations/email/test',
                       json={'to_email': 'admin@example.com'}, timeout=10)
    r.raise_for_status()
    assert r.json()['sent_to'] == 'admin@example.com'
    assert len(smtp_mock) == 1
    assert smtp_mock[0]['To'] == 'admin@example.com'


def test_email_qr_endpoint_enqueues_then_worker_sends(app_server, smtp_mock):
    base = app_server['base_url']
    _save_smtp(base)
    user_id = _create_user(base)

    r = requests.post(f'{base}/api/users/{user_id}/email-qr', timeout=10)
    r.raise_for_status()
    assert r.json().get('ok') is True

    # Tests don't run the worker loop; pop + process synchronously.
    from web.services import email as email_svc
    job = email_svc._pop_next_queued_job()
    assert job is not None
    email_svc._process_job(job)

    assert len(smtp_mock) == 1
    msg = smtp_mock[0]
    assert msg['To'] == 'resident@example.com'
    assert msg['Subject']
    # multipart with a CID-embedded image
    parts = list(msg.iter_parts())
    assert any('image/png' in (p.get_content_type() or '') for p in parts) or \
           any(any('image/png' in (sp.get_content_type() or '')
                   for sp in p.iter_parts())
                for p in parts), 'expected an inline image part'

    # users.last_email_sent_at populated
    app = app_server['app']
    user = app.db.get_user(user_id)
    assert user['last_email_sent_at']


def test_email_qr_rejected_without_email(app_server, smtp_mock):
    base = app_server['base_url']
    _save_smtp(base)
    # Create a user via the manual API but with empty email - the manual
    # route refuses empty emails, so insert directly via db helper to
    # mimic an external_user without email.
    app = app_server['app']
    uid = app.db.create_user('No Email', None, 'Unit 2')
    r = requests.post(f'{base}/api/users/{uid}/email-qr', timeout=10)
    assert r.status_code == 400
    assert 'no email' in r.json()['error'].lower()


def test_email_qr_blocked_when_unconfigured(app_server):
    base = app_server['base_url']
    user_id = _create_user(base)
    r = requests.post(f'{base}/api/users/{user_id}/email-qr', timeout=10)
    assert r.status_code == 400
    assert 'not configured' in r.json()['error'].lower()


def test_email_qr_batch_enqueues_all_with_emails(app_server, smtp_mock):
    """Bulk endpoint enqueues one job per valid user; the worker then
    drains them in order. Skips users without email."""
    base = app_server['base_url']
    _save_smtp(base)
    a = _create_user(base, email='a@example.com')
    b = _create_user(base, email='b@example.com')
    c = _create_user(base, email='c@example.com')
    app = app_server['app']
    # Sneak in a no-email user (manual route would refuse) to verify skip.
    no_email = app.db.create_user('No Email', None, '')

    r = requests.post(f'{base}/api/users/email-qr-batch',
                       json={'user_ids': [a, b, c, no_email]}, timeout=10)
    r.raise_for_status()
    body = r.json()
    assert body['queued'] == 3
    assert body['skipped'] == 1

    # Drain the queue synchronously (test-process worker isn't running).
    from web.services import email as email_svc
    for _ in range(3):
        job = email_svc._pop_next_queued_job()
        assert job is not None
        email_svc._process_job(job)
    assert email_svc._pop_next_queued_job() is None

    sent_to = sorted(m['To'] for m in smtp_mock)
    assert sent_to == ['a@example.com', 'b@example.com', 'c@example.com']


def test_email_disconnect_clears_creds_keeps_templates(app_server, smtp_mock):
    base = app_server['base_url']
    payload = {**SMTP_CFG,
                'subject_template': 'Custom subject for {{ full_name }}'}
    requests.post(f'{base}/api/integrations/email',
                   json=payload, timeout=10).raise_for_status()
    s = requests.get(f'{base}/api/integrations/email').json()
    assert s['configured'] is True
    assert s['subject_template']

    r = requests.delete(f'{base}/api/integrations/email')
    r.raise_for_status()
    s = requests.get(f'{base}/api/integrations/email').json()
    assert s['configured'] is False
    # Template survives so the admin doesn't have to re-author it.
    assert 'Custom subject' in s['subject_template']


def test_smtp_save_keeps_password_when_blank(app_server, smtp_mock):
    base = app_server['base_url']
    _save_smtp(base)   # initial save with password
    # Resave with empty password - backend must keep the old one.
    _save_smtp(base, password='')
    s = requests.get(f'{base}/api/integrations/email').json()
    assert s['has_password'] is True   # still configured
    # And the test endpoint still works (proves password wasn't blanked).
    r = requests.post(f'{base}/api/integrations/email/test',
                       json={'to_email': 'admin@example.com'}, timeout=10)
    r.raise_for_status()
