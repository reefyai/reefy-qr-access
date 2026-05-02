"""Browser-driven (Playwright) tests for the qr-access UI.

Validates the user-facing flow: open settings, configure Buildium, click
Sync, then visit the dashboard and confirm rows render with correct source
badges + inactive styling.
"""

from __future__ import annotations

import requests

from .conftest import SAMPLE_OWNERS, install_buildium_routes


CREDS = {
    'client_id': 'fake-client-id',
    'client_secret': 'fake-secret',
    'base_url': 'https://api.buildium.com/v1',
}


def _seed_synced_users(base_url):
    """Save creds + run sync via API so the dashboard has rows to render."""
    r = requests.post(f'{base_url}/api/integrations/buildium',
                      json=CREDS, timeout=10)
    r.raise_for_status()
    r = requests.post(f'{base_url}/api/integrations/buildium/sync', timeout=30)
    r.raise_for_status()


def test_settings_shows_version(app_server, page):
    page.goto(f"{app_server['base_url']}/settings")
    version = page.locator('[data-testid="app-version"]').text_content()
    assert version and version.startswith('v'), \
        f'expected version starting with v, got {version!r}'


def test_dashboard_renders_buildium_badge_and_columns(
        app_server, buildium_mock, page):
    _seed_synced_users(app_server['base_url'])
    page.goto(f"{app_server['base_url']}/")

    # All synced users get the Buildium source badge
    badges = page.locator('.badge-source-buildium').all_text_contents()
    assert len(badges) == 4
    assert all(b == 'Buildium' for b in badges)

    # Phone + unit columns render data we put in the fixture
    body = page.locator('#users-table').inner_text()
    assert 'Alice Owner' in body
    assert 'Unit A' in body
    assert '(555) 555-0100' in body


def test_dashboard_marks_inactive_after_disappearance(
        app_server, buildium_mock, page):
    _seed_synced_users(app_server['base_url'])

    # Bob disappears, re-sync, refresh dashboard
    buildium_mock.reset()
    install_buildium_routes(
        buildium_mock,
        owners=[o for o in SAMPLE_OWNERS if o['Id'] != 2002])
    requests.post(
        f"{app_server['base_url']}/api/integrations/buildium/sync",
        timeout=30).raise_for_status()

    page.goto(f"{app_server['base_url']}/")
    bob_row = page.locator('tr', has_text='Bob Owner').first
    bob_class = bob_row.get_attribute('class') or ''
    assert 'user-row-inactive' in bob_class
    assert '(inactive)' in bob_row.inner_text()


def test_settings_buildium_form_save_and_sync_via_ui(
        app_server, buildium_mock, page):
    page.goto(f"{app_server['base_url']}/settings")
    page.locator('#buildium-client-id').fill(CREDS['client_id'])
    page.locator('#buildium-client-secret').fill(CREDS['client_secret'])
    page.get_by_role('button', name='Save & Sync').click()

    # Wait for the success line to render in the status panel
    page.wait_for_function(
        "() => document.getElementById('buildium-status').textContent.includes('Last sync')",
        timeout=15000)

    # Visit dashboard to confirm users showed up
    page.goto(f"{app_server['base_url']}/")
    badges = page.locator('.badge-source-buildium').all_text_contents()
    assert len(badges) == 4
