"""API contract tests for validation before door creation."""

import requests


CAMERA = {
    'ip': '192.0.2.10',
    'uuid': '00000000-0000-4000-8000-000000000001',
    'name': 'Synthetic Camera',
    'hardware': 'Test Model',
    'xaddr': 'http://192.0.2.10:2020/onvif/device_service',
}


def _payload():
    return {
        'name': 'Test Door',
        'camera_uuid': CAMERA['uuid'],
        'camera_user': 'operator',
        'camera_pass': 'synthetic-password',
        'camera_path': '/onvif/main',
        'camera_port': 554,
        'opener_type': 'onvif',
    }


def test_rejected_credentials_do_not_create_door(app_server, monkeypatch):
    app_module = app_server['app']
    app_module.db.upsert_cameras([CAMERA])
    monkeypatch.setattr(
        app_module, 'validate_camera',
        lambda *args, **kwargs: {
            'ok': False,
            'status': 422,
            'error': 'Camera rejected the username or password.',
        })

    response = requests.post(
        f"{app_server['base_url']}/api/doors", json=_payload(), timeout=5)

    assert response.status_code == 422
    assert 'username or password' in response.json()['error']
    assert app_module.db.get_doors() == []


def test_unreachable_camera_does_not_create_door(app_server, monkeypatch):
    app_module = app_server['app']
    app_module.db.upsert_cameras([CAMERA])
    monkeypatch.setattr(
        app_module, 'validate_camera',
        lambda *args, **kwargs: {
            'ok': False,
            'status': 503,
            'error': 'Camera is unreachable.',
        })

    response = requests.post(
        f"{app_server['base_url']}/api/doors", json=_payload(), timeout=5)

    assert response.status_code == 503
    assert app_module.db.get_doors() == []


def test_successful_validation_creates_door(app_server, monkeypatch):
    app_module = app_server['app']
    app_module.db.upsert_cameras([CAMERA])
    monkeypatch.setattr(
        app_module, 'validate_camera',
        lambda *args, **kwargs: {
            'ok': True,
            'status': 200,
            'error': None,
        })

    response = requests.post(
        f"{app_server['base_url']}/api/doors", json=_payload(), timeout=5)

    assert response.status_code == 201
    assert len(app_module.db.get_doors()) == 1
