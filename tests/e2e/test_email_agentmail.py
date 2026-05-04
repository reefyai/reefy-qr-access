"""API-level e2e for the AgentMail provider.

Mocks the AgentMail HTTP API via the `responses` library (same pattern
used for Buildium). Does not hit the real network. Worker is invoked
synchronously via the service module so we don't have to wait on the
poll loop.

The admin pre-provisions an inbox in the AgentMail console; we only
ever POST to /v0/inboxes/{from_email}/messages/send. There is no
auto-provisioning code path to test.
"""

from __future__ import annotations

import base64
import json

import pytest
import requests
import responses


AM_BASE = 'https://api.agentmail.to/v0'
INBOX = 'test-inbox@agentmail.to'

AGENTMAIL_CFG = {
    'provider': 'agentmail',
    'agentmail_api_key': 'sk_test_abc123',
    'from_email': INBOX,
    'from_name': 'Test Sender',
}


def _save_cfg(base_url, **overrides):
    payload = {**AGENTMAIL_CFG, **overrides}
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


@pytest.fixture
def agentmail_mock():
    """Activates `responses` and yields it; passes through localhost so
    the test's own requests to Flask aren't intercepted."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add_passthru('http://127.0.0.1')
        yield rsps


def test_agentmail_save_config(app_server, agentmail_mock):
    base = app_server['base_url']
    _save_cfg(base)
    s = requests.get(f'{base}/api/integrations/email').json()
    assert s['configured'] is True
    assert s['provider'] == 'agentmail'
    assert s['has_agentmail_api_key'] is True
    assert s['from_email'] == INBOX
    # API key never round-trips
    assert 'agentmail_api_key' not in s


def test_agentmail_save_requires_api_key_and_from_email(app_server, agentmail_mock):
    base = app_server['base_url']
    # Missing api_key
    r = requests.post(f'{base}/api/integrations/email', json={
        'provider': 'agentmail',
        'from_email': INBOX,
    }, timeout=10)
    assert r.status_code == 400
    # Missing from_email
    r = requests.post(f'{base}/api/integrations/email', json={
        'provider': 'agentmail',
        'agentmail_api_key': 'sk_test_abc123',
    }, timeout=10)
    assert r.status_code == 400


def test_agentmail_test_endpoint_sends_via_inbox(app_server, agentmail_mock):
    base = app_server['base_url']
    _save_cfg(base)

    agentmail_mock.add(responses.POST,
                        f'{AM_BASE}/inboxes/{INBOX}/messages/send',
                        json={'message_id': 'msg_1'}, status=200)

    r = requests.post(f'{base}/api/integrations/email/test',
                       json={'to_email': 'admin@example.com'}, timeout=10)
    r.raise_for_status()
    assert r.json()['sent_to'] == 'admin@example.com'

    sends = [c for c in agentmail_mock.calls
              if 'messages/send' in c.request.url]
    assert len(sends) == 1
    body = json.loads(sends[0].request.body)
    assert body['to'] == 'admin@example.com'
    assert body['attachments'][0]['content_id'] == 'qrcode'
    assert sends[0].request.headers['Authorization'] == 'Bearer sk_test_abc123'


def test_agentmail_never_calls_create_inbox(app_server, agentmail_mock):
    """Inbox-scoped keys 403 on POST /v0/inboxes - we must never hit
    that endpoint. The admin pre-creates the inbox in the AgentMail
    console and pastes the email into From email."""
    base = app_server['base_url']
    _save_cfg(base)

    agentmail_mock.add(responses.POST,
                        f'{AM_BASE}/inboxes/{INBOX}/messages/send',
                        json={'message_id': 'msg_1'}, status=200)

    r = requests.post(f'{base}/api/integrations/email/test',
                       json={'to_email': 'admin@example.com'}, timeout=10)
    r.raise_for_status()

    provisions = [c for c in agentmail_mock.calls
                   if c.request.url.endswith('/inboxes')
                   and c.request.method == 'POST']
    assert provisions == []


def test_agentmail_email_qr_enqueues_then_worker_sends(app_server, agentmail_mock):
    base = app_server['base_url']
    _save_cfg(base)
    user_id = _create_user(base)

    agentmail_mock.add(responses.POST,
                        f'{AM_BASE}/inboxes/{INBOX}/messages/send',
                        json={'message_id': 'msg_1'}, status=200)

    r = requests.post(f'{base}/api/users/{user_id}/email-qr', timeout=10)
    r.raise_for_status()

    from web.services import email as email_svc
    job = email_svc._pop_next_queued_job()
    assert job is not None
    email_svc._process_job(job)

    sends = [c for c in agentmail_mock.calls
              if 'messages/send' in c.request.url]
    assert len(sends) == 1
    body = json.loads(sends[0].request.body)
    assert body['to'] == 'resident@example.com'
    # Inline QR attachment with cid:qrcode
    att = body['attachments'][0]
    assert att['content_disposition'] == 'inline'
    assert att['content_id'] == 'qrcode'
    # Base64 PNG payload
    raw = base64.b64decode(att['content'])
    assert raw[:8] == b'\x89PNG\r\n\x1a\n'

    app = app_server['app']
    user = app.db.get_user(user_id)
    assert user['last_email_sent_at']


def test_agentmail_save_keeps_api_key_when_blank(app_server, agentmail_mock):
    base = app_server['base_url']
    _save_cfg(base)
    # Resave with blank api_key - backend keeps the prior value.
    _save_cfg(base, agentmail_api_key='')
    s = requests.get(f'{base}/api/integrations/email').json()
    assert s['has_agentmail_api_key'] is True


def test_agentmail_disconnect_clears_creds_keeps_templates(app_server, agentmail_mock):
    base = app_server['base_url']
    payload = {**AGENTMAIL_CFG,
                'subject_template': 'Custom subject for {{ full_name }}'}
    requests.post(f'{base}/api/integrations/email',
                   json=payload, timeout=10).raise_for_status()
    s = requests.get(f'{base}/api/integrations/email').json()
    assert s['configured'] is True

    requests.delete(f'{base}/api/integrations/email').raise_for_status()
    s = requests.get(f'{base}/api/integrations/email').json()
    assert s['configured'] is False
    assert s['has_agentmail_api_key'] is False
    assert 'Custom subject' in s['subject_template']
