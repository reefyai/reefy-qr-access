"""Shared fixtures for qr-access e2e tests.

Strategy: spin Flask up in a background thread inside the test process so
`responses` HTTP patches (which monkey-patch the requests library) apply to
the Buildium calls Flask makes. Each test gets a fresh temp SQLite via
QR_CONFIG_DIR.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import requests


# Make the qr-access package importable.
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _free_port() -> int:
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def app_server(tmp_path, monkeypatch):
    """Start Flask in a thread; tear down after the test.

    Yields a dict: {base_url, config_dir, app_module}.
    """
    config_dir = tmp_path / 'config'
    config_dir.mkdir()
    monkeypatch.setenv('QR_CONFIG_DIR', str(config_dir))
    monkeypatch.setenv('QR_ADMIN_PASSWORD', '')  # disable login gate

    # Importing app inside the fixture so QR_CONFIG_DIR is honoured by
    # the module-level db.DB_PATH. Clear ALL web.* modules so cross-test
    # state (e.g. web.services.buildium's cached `db` reference) doesn't
    # write to the prior test's SQLite path.
    for name in [n for n in list(sys.modules) if n == 'web' or n.startswith('web.')]:
        del sys.modules[name]
    from web import app as app_module

    app_module.db.init_db()
    port = _free_port()

    server = app_module.app
    thread = threading.Thread(
        target=server.run,
        kwargs=dict(host='127.0.0.1', port=port, debug=False,
                    use_reloader=False, threaded=True),
        daemon=True,
    )
    thread.start()

    base_url = f'http://127.0.0.1:{port}'
    # Wait for socket-accept readiness.
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            requests.get(f'{base_url}/', timeout=0.5)
            break
        except requests.RequestException:
            time.sleep(0.1)
    else:
        raise RuntimeError('flask test server never came up')

    yield {
        'base_url': base_url,
        'config_dir': str(config_dir),
        'app': app_module,
    }
    # No clean shutdown for the dev server; daemon thread dies with the
    # test process. Each test gets a fresh port + fresh module state.


# Sample Buildium fixtures: 3 owners + 1 tenant in one association
SAMPLE_ASSOC = {'Id': 1000, 'Name': 'Test HOA', 'IsActive': True}
SAMPLE_UNITS = [
    {'Id': 5001, 'AssociationId': 1000, 'UnitNumber': 'Unit A'},
    {'Id': 5002, 'AssociationId': 1000, 'UnitNumber': 'Unit B'},
]


def _person(pid, fname, lname, email, unit_id, phone='(555) 555-0100',
            alt_email='', phone2=None):
    phones = [{'Number': phone, 'Type': 'Cell'}]
    if phone2:
        phones.append({'Number': phone2, 'Type': 'Home'})
    return {
        'Id': pid,
        'FirstName': fname, 'LastName': lname,
        'Email': email, 'AlternateEmail': alt_email,
        'PhoneNumbers': phones,
        'PrimaryAddress': {'AddressLine1': '123 Main', 'City': 'Anytown'},
        'OwnershipAccounts': [
            {'AssociationId': 1000, 'UnitId': unit_id, 'Status': 'Active'}
        ],
        'OccupiesUnit': True,
    }


SAMPLE_OWNERS = [
    _person(2001, 'Alice', 'Owner', 'alice@example.com', 5001),
    _person(2002, 'Bob', 'Owner', 'bob@example.com', 5001, phone2='(555) 555-0200'),
    _person(2003, 'Carol', 'Owner', 'carol@example.com', 5002,
            alt_email='carol2@example.com'),
]
SAMPLE_TENANTS = [
    _person(3001, 'Dan', 'Tenant', 'dan@example.com', 5002),
]


def install_buildium_routes(rsps, owners=None, tenants=None,
                              base='https://api.buildium.com/v1'):
    """Register `responses` mocks for the full Buildium surface the sync
    orchestrator hits. Pass owners/tenants overrides to simulate
    add/remove/reappear scenarios."""
    owners = SAMPLE_OWNERS if owners is None else owners
    tenants = SAMPLE_TENANTS if tenants is None else tenants

    def page(items):
        # responses matches by query string; we just always return the full
        # set on offset=0 and an empty list otherwise so pagination
        # terminates after one round.
        def cb(request):
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(request.url).query)
            offset = int(qs.get('offset', ['0'])[0])
            import json
            if offset == 0:
                return (200, {}, json.dumps(items))
            return (200, {}, json.dumps([]))
        return cb

    import responses as r
    # Always re-add localhost passthrough; tests typically call
    # rsps.reset() before install_buildium_routes() and reset wipes
    # passthrough rules along with mocks.
    rsps.add_passthru('http://127.0.0.1')
    rsps.add_callback(r.GET, f'{base}/associations',
                      callback=page([SAMPLE_ASSOC]))
    rsps.add_callback(r.GET, f'{base}/associations/units',
                      callback=page(SAMPLE_UNITS))
    rsps.add_callback(r.GET, f'{base}/associations/owners',
                      callback=page(owners))
    rsps.add_callback(r.GET, f'{base}/associations/tenants',
                      callback=page(tenants))
    # Rentals tree stubbed empty
    rsps.add_callback(r.GET, f'{base}/rentals', callback=page([]))


@pytest.fixture
def smtp_mock(monkeypatch):
    """Patch smtplib.SMTP so tests don't open a real socket. Returns a
    list that captures every EmailMessage the code tried to send."""
    import smtplib
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            self.host = host
            self.port = port
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def starttls(self, context=None): pass
        def login(self, u, p): self.user = u
        def send_message(self, msg):
            sent.append(msg)

    monkeypatch.setattr(smtplib, 'SMTP', FakeSMTP)
    return sent


@pytest.fixture
def buildium_mock():
    """Activates `responses` and yields it so tests can reconfigure mid-run.
    Localhost is passthrough so the test's own requests to Flask aren't mocked.
    """
    import responses
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add_passthru('http://127.0.0.1')
        install_buildium_routes(rsps)
        yield rsps
