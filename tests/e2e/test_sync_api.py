"""API-level tests for Buildium sync.

Covers the bug-prone parts: idempotency, inactivate-on-disappearance,
reactivate-on-reappearance, manual users untouched by sync, bad creds rejected
without saving.
"""

from __future__ import annotations

import json

import requests

from .conftest import (
    SAMPLE_OWNERS, SAMPLE_TENANTS, install_buildium_routes,
)


CREDS = {
    'client_id': 'fake-client-id',
    'client_secret': 'fake-secret',
    'base_url': 'https://api.buildium.com/v1',
}


def _save_creds(base_url):
    r = requests.post(f'{base_url}/api/integrations/buildium',
                      json=CREDS, timeout=10)
    r.raise_for_status()


def _sync(base_url):
    r = requests.post(f'{base_url}/api/integrations/buildium/sync', timeout=30)
    r.raise_for_status()
    return r.json()


def _get_users(app_module):
    return app_module.db.get_users()


def test_first_sync_creates_users(app_server, buildium_mock):
    _save_creds(app_server['base_url'])
    result = _sync(app_server['base_url'])

    assert result['errors'] == []
    assert result['external_users']['created'] == 4   # 3 owners + 1 tenant
    assert result['users']['created'] == 4
    assert result['associations'] == 1

    users = _get_users(app_server['app'])
    assert len(users) == 4
    sources = {u['created_via'] for u in users}
    assert sources == {'sync:buildium'}
    # Common-column data made it through
    by_name = {u['full_name']: u for u in users}
    assert by_name['Alice Owner']['email'] == 'alice@example.com'
    assert by_name['Bob Owner']['phone_primary'] == '(555) 555-0100'
    assert by_name['Carol Owner']['alternate_email'] == 'carol2@example.com'
    assert by_name['Alice Owner']['unit_label'] == 'Unit A'


def test_first_sync_auto_issues_tokens(app_server, buildium_mock):
    """Every new user from a Buildium sync should get one active token,
    same as users created via the manual API. Without this, residents
    show 'No active QR codes' after sync."""
    _save_creds(app_server['base_url'])
    _sync(app_server['base_url'])

    app = app_server['app']
    for u in _get_users(app):
        tokens = app.db.get_user_tokens(u['id'])
        assert len(tokens) == 1, (
            f"user {u['full_name']!r} expected 1 token, got {len(tokens)}")
        assert tokens[0]['active'] == 1
        assert 'Buildium' in tokens[0]['comment']


def test_second_sync_is_idempotent(app_server, buildium_mock):
    _save_creds(app_server['base_url'])
    _sync(app_server['base_url'])
    result = _sync(app_server['base_url'])

    assert result['external_users']['created'] == 0
    assert result['external_users']['updated'] == 4
    assert result['users']['created'] == 0
    assert result['users']['inactivated'] == 0


def test_disappeared_user_inactivates_with_tokens(app_server, buildium_mock):
    _save_creds(app_server['base_url'])
    _sync(app_server['base_url'])

    app = app_server['app']
    users = _get_users(app)
    bob = next(u for u in users if u['full_name'] == 'Bob Owner')
    # Generate a token for Bob so we can verify cascade revocation
    app.db.create_token_for_user(bob['id'], 'bob-token-xyz')

    # Bob disappears from Buildium
    buildium_mock.reset()
    install_buildium_routes(
        buildium_mock,
        owners=[o for o in SAMPLE_OWNERS if o['Id'] != 2002])

    result = _sync(app_server['base_url'])
    assert result['external_users']['inactivated'] == 1
    assert result['users']['inactivated'] == 1
    # Bob has 2 tokens at this point: the one auto-issued by sync on
    # initial create + the one the test planted above. Both revoke.
    assert result['tokens_revoked'] == 2

    bob_after = next(u for u in _get_users(app) if u['id'] == bob['id'])
    assert bob_after['is_active'] == 0
    # Verify all tokens actually revoked
    tokens = app.db.get_user_tokens(bob['id'])
    assert all(t['active'] == 0 for t in tokens)


def test_reappeared_user_reactivates_but_tokens_stay_revoked(
        app_server, buildium_mock):
    _save_creds(app_server['base_url'])
    _sync(app_server['base_url'])

    app = app_server['app']
    bob = next(u for u in _get_users(app) if u['full_name'] == 'Bob Owner')
    app.db.create_token_for_user(bob['id'], 'bob-token-xyz')

    # Disappear, sync, reappear, sync
    buildium_mock.reset()
    install_buildium_routes(
        buildium_mock,
        owners=[o for o in SAMPLE_OWNERS if o['Id'] != 2002])
    _sync(app_server['base_url'])

    buildium_mock.reset()
    install_buildium_routes(buildium_mock)   # full set back
    result = _sync(app_server['base_url'])

    assert result['external_users']['reactivated'] == 1
    assert result['users']['reactivated'] == 1

    bob_after = next(u for u in _get_users(app) if u['id'] == bob['id'])
    assert bob_after['is_active'] == 1
    # Tokens stay revoked - admin must re-issue
    tokens = app.db.get_user_tokens(bob['id'])
    assert all(t['active'] == 0 for t in tokens), \
        'reactivation should NOT auto-restore physical access'


def test_manual_user_untouched_by_sync(app_server, buildium_mock):
    base = app_server['base_url']
    # Create a manual user via the public API
    r = requests.post(f'{base}/api/users', json={
        'full_name': 'Manual McManual',
        'email': 'manual@example.com',
        'address': '999 Other St',
    }, timeout=10)
    r.raise_for_status()
    manual_id = r.json()['user']['id']

    _save_creds(base)
    _sync(base)

    # Even after disappear sync (which inactivates synced users), manual
    # stays put.
    buildium_mock.reset()
    install_buildium_routes(buildium_mock, owners=[], tenants=[])
    _sync(base)

    app = app_server['app']
    manual = next(u for u in _get_users(app) if u['id'] == manual_id)
    assert manual['is_active'] == 1
    assert manual['created_via'] == 'manual'


def test_manual_user_with_phone(app_server):
    """POST /api/users persists phone + it surfaces in the table read."""
    base = app_server['base_url']
    r = requests.post(f'{base}/api/users', json={
        'full_name': 'Phoneful Person',
        'email': 'phone@example.com',
        'phone': '(555) 123-4567',
        'address': '99 Test Ln',
    }, timeout=10)
    r.raise_for_status()

    app = app_server['app']
    user = next(u for u in app.db.get_users()
                if u['full_name'] == 'Phoneful Person')
    assert user['phone_primary'] == '(555) 123-4567'
    assert user['address'] == '99 Test Ln'
    # And one auto-issued token (same path as Buildium sync now)
    tokens = app.db.get_user_tokens(user['id'])
    assert len(tokens) == 1 and tokens[0]['active'] == 1


def test_test_connection_endpoint(app_server, buildium_mock):
    """POST /api/integrations/buildium/test must return 200 + ok=True
    when creds work. Regression: jsonify(ok=True, **result) collided with
    test_connection's own 'ok' key and 500'd."""
    r = requests.post(
        f"{app_server['base_url']}/api/integrations/buildium/test",
        json=CREDS, timeout=10)
    assert r.status_code == 200, f'unexpected status: {r.status_code}, body={r.text}'
    body = r.json()
    assert body.get('ok') is True
    assert 'sample_count' in body


def test_bad_creds_rejected_without_saving(app_server, buildium_mock):
    # Override /associations to return 401 for the validation probe.
    # reset() drops all routes including the localhost passthrough, so
    # re-add it before adding the new mock.
    import responses
    base = 'https://api.buildium.com/v1'
    buildium_mock.reset()
    buildium_mock.add_passthru('http://127.0.0.1')
    buildium_mock.add(responses.GET, f'{base}/associations', status=401)

    r = requests.post(f"{app_server['base_url']}/api/integrations/buildium",
                      json=CREDS, timeout=10)
    assert r.status_code == 400
    assert 'Authentication' in r.json().get('error', '') or \
           'auth' in r.json().get('error', '').lower()

    # Status should still report not-configured
    s = requests.get(f"{app_server['base_url']}/api/integrations/buildium",
                     timeout=10).json()
    assert s['configured'] is False


def test_version_endpoint(app_server):
    r = requests.get(f"{app_server['base_url']}/api/version", timeout=5)
    r.raise_for_status()
    v = r.json()['version']
    # YYYY.MM.DD-NN format (or 0.0.0-dev fallback)
    assert v
    assert '-' in v
