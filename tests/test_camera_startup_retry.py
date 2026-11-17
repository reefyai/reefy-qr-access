"""Delayed networking through startup, reader threads and the snapshot API.

Only hardware boundaries (discovery, decoder and actuator) are synthetic.
The production main loop, retry handling, registry and web/SQLite path run.
"""
import copy
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

import numpy as np
import qr_live as qr


CAMERA = {'ip': '192.0.2.20', 'uuid': 'synthetic-target-camera',
          'name': 'Synthetic target', 'hardware': 'Test camera',
          'xaddr': 'http://192.0.2.20/onvif/device_service'}
OTHER = {**CAMERA, 'ip': '192.0.2.99', 'uuid': 'synthetic-other-camera'}
DOOR = {'name': 'Delayed door', 'camera': 'onvif:uuid:synthetic-target-camera',
        'camera_user': 'operator', 'camera_pass': 'synthetic-password',
        'camera_path': '/stream2', 'camera_port': 554, 'opener': 'onvif',
        'relay_token': 'synthetic-relay', 'open_seconds': 7,
        'tokens': ['synthetic-allowed-token']}


class StartupHarness:
    def __enter__(self):
        self.stack = ExitStack()
        self.tmp = tempfile.TemporaryDirectory()
        self.ready = threading.Event()
        self.seen_ready = threading.Event()
        self.seen_delayed = threading.Event()
        self.reload = threading.Event()
        self.errors = []
        self.discovery_calls = 0
        self.urls = []
        self.config = Path(self.tmp.name) / 'doors.yaml'
        self.config.write_text(json.dumps({'doors': [
            {'name': 'Ready door', 'camera': 'rtsp://192.0.2.30/healthy',
             'shelly': '192.0.2.31', 'tokens': []}, DOOR]}))
        self.original_config = self.config.read_bytes()
        def discovery(timeout=3):
            self.discovery_calls += 1
            return [OTHER, CAMERA] if self.ready.is_set() else [OTHER]
        harness = self
        class Frames:
            width, height, fps = 96, 64, 15
            def __init__(self, delayed): self.delayed = delayed
            def read(self):
                time.sleep(.005)
                return True, np.full((64, 96, 3), 200 if self.delayed else 100, dtype=np.uint8)
            def release(self): pass
        def open_reader(url, *args):
            harness.urls.append(url)
            assert url is not None, 'An unresolved camera must not open a default/local source'
            return Frames('192.0.2.20' in url)
        class Detector:
            def info(self): return {'detect': 'synthetic', 'detect_device': 'cpu'}
            def detect(self, frame, **kwargs):
                (harness.seen_delayed if int(frame[0, 0, 0]) == 200 else harness.seen_ready).set()
                return []
        patches = [
            mock.patch.object(qr, '_running', True),
            mock.patch.object(qr, '_camera_readers', []),
            mock.patch.object(qr, '_LIVE_DOORS', {}),
            mock.patch.object(qr, '_reload_event', self.reload),
            mock.patch.object(qr, 'HAS_GPU', False),
            mock.patch.object(qr, 'CAMERA_RETRY_DELAYS_SECONDS', (.02, .05, .1)),
            mock.patch.object(qr.signal, 'signal'),
            mock.patch.object(qr, 'discover_shelly', return_value={}),
            mock.patch.object(qr, 'discover_onvif_cameras', side_effect=discovery),
            mock.patch.object(qr, 'DoorController'),
            mock.patch.object(qr, 'OnvifRelayController'),
            mock.patch.object(qr, 'create_decoder', return_value=lambda frame: []),
            mock.patch.object(qr, 'VideoLogger'),
            mock.patch.object(qr, '_cpu_name', return_value='Synthetic CPU'),
            mock.patch.object(qr.pipeline, 'detect_pipeline_backend', return_value='cpu'),
            mock.patch.object(qr.pipeline, 'build_detector', return_value=(Detector(), 'cpu')),
            mock.patch.object(qr.video_decode, 'open_reader', side_effect=open_reader),
            mock.patch.object(qr.sys, 'argv', ['qr_live.py', '--config', str(self.config), '--model-size', 'n'])]
        for patch in patches: self.stack.enter_context(patch)
        self.relay = qr.OnvifRelayController
        self.decoder = qr.video_decode.open_reader
        def main():
            try: qr.main()
            except BaseException as exc: self.errors.append(exc)
        self.thread = threading.Thread(target=main, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        self.reload.set()
        qr._running = False
        for reader in qr._camera_readers: reader.stop()
        self.thread.join(5)
        if self.thread.is_alive(): raise AssertionError('Detector did not stop')

    def __exit__(self, *args):
        try: self.stop()
        finally:
            self.stack.close()
            self.tmp.cleanup()


class DelayedNetworkTests(unittest.TestCase):
    def test_startup_recovers_delayed_camera_and_snapshot_without_blocking_peer(self):
        with StartupHarness() as h:
            self.assertTrue(h.seen_ready.wait(5), h.errors)
            pending = qr.get_live_door(DOOR['name'])
            self.assertIsNotNone(pending, 'Configured doors must not disappear after one startup miss')
            self.assertIsNone(pending.camera.get_frame()[0])
            self.assertGreaterEqual(h.discovery_calls, 2)
            h.relay.assert_not_called()
            from web import app as app_module, db
            with mock.patch.object(db, 'DB_DIR', h.tmp.name), mock.patch.object(db, 'DB_PATH', str(Path(h.tmp.name) / 'state.db')), mock.patch.object(db, '_local', threading.local()), mock.patch.object(app_module, 'ADMIN_PASSWORD', ''):
                db.init_db()
                try:
                    door_id = db.create_door(DOOR['name'], CAMERA['uuid'], 'operator', 'synthetic-password', '/stream2', 554, '', '', 7, 'onvif', 'synthetic-relay')
                    before = copy.deepcopy(db.get_doors())
                    client = app_module.app.test_client()
                    self.assertEqual(503, client.get(f'/api/doors/{door_id}/snapshot.jpg').status_code)
                    h.ready.set()
                    self.assertTrue(h.seen_delayed.wait(5), h.errors)
                    response = client.get(f'/api/doors/{door_id}/snapshot.jpg')
                    self.assertEqual(200, response.status_code)
                    self.assertTrue(response.data.startswith(b'\xff\xd8'))
                    self.assertEqual(before, db.get_doors())
                finally:
                    db.get_db().close()
            self.assertEqual(set(DOOR['tokens']), pending.valid_tokens)
            self.assertEqual(7, pending.open_seconds)
            h.relay.assert_called_once_with(DOOR['name'], CAMERA['ip'], username='operator', password='synthetic-password', open_seconds=7, relay_token='synthetic-relay', device_url=CAMERA['xaddr'])
            h.relay.return_value.open.assert_not_called()
            self.assertEqual(h.original_config, h.config.read_bytes())
            self.assertFalse(h.errors)

    def test_unrelated_identity_is_never_adopted_and_reload_cancels_pending_reader(self):
        with StartupHarness() as h:
            self.assertTrue(h.seen_ready.wait(5), h.errors)
            self.assertGreaterEqual(h.discovery_calls, 2)
            pending = qr.get_live_door(DOOR['name'])
            self.assertIsNotNone(pending)
            h.relay.assert_not_called()
            self.assertFalse(any('192.0.2.99' in url for url in h.urls))
            h.stop()
            calls = h.discovery_calls
            h.ready.set()
            time.sleep(.15)
            self.assertEqual(calls, h.discovery_calls)
            self.assertTrue(pending.camera._stopped)
            self.assertIsNone(qr.get_live_door(DOOR['name']))
            h.relay.assert_not_called()
            self.assertEqual(h.original_config, h.config.read_bytes())


class ReaderResolutionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(qr, '_running', True))
        self.stack.enter_context(mock.patch.object(qr, '_reload_event', None))

    def test_unresolved_retry_backoff_is_capped_and_never_opens_default_camera(self):
        waits = []
        reader = qr.CameraReader(None, url_builder=lambda: None)
        def wait(delay):
            waits.append(delay)
            if len(waits) == 9: reader.stop()
        reader._stop_event.wait = wait
        with mock.patch.object(qr.video_decode, 'open_reader', return_value=None) as decoder:
            reader.run()
        decoder.assert_not_called()
        self.assertEqual([2, 5, 10, 30, 60, 120, 300, 300, 300], waits)

    def test_stop_during_discovery_does_not_bind_late_camera_or_relay(self):
        entered, release = threading.Event(), threading.Event()
        door = qr.DoorConfig('Synthetic delayed door', None, None, {'synthetic-token'})
        def discovery(**kwargs):
            entered.set()
            release.wait(3)
            return [CAMERA]
        with mock.patch.object(qr, 'discover_onvif_cameras', side_effect=discovery), mock.patch.object(qr, 'OnvifRelayController') as relay, mock.patch.object(qr.video_decode, 'open_reader') as decoder:
            door.camera = qr.CameraReader(None, url_builder=qr.unresolved_camera_builder(door, DOOR))
            door.camera.start()
            try:
                self.assertTrue(entered.wait(2))
                door.camera.stop()
            finally:
                release.set()
                door.camera.join(3)
            self.assertFalse(door.camera.is_alive())
            relay.assert_not_called()
            decoder.assert_not_called()
            self.assertIsNone(door.camera_url)

    def test_rejected_credentials_pause_until_configuration_changes(self):
        rejected = threading.Event()
        def auth():
            rejected.set()
            raise qr.CameraAuthenticationError('synthetic rejection')
        door = qr.DoorConfig('Synthetic delayed door', None, None, set())
        with mock.patch.object(qr, 'discover_onvif_cameras', return_value=[CAMERA]), mock.patch.object(qr, 'build_rtsp_url', return_value=('rtsp://192.0.2.20/stream2', auth)), mock.patch.object(qr, 'OnvifRelayController') as relay, mock.patch.object(qr.video_decode, 'open_reader') as decoder:
            door.camera = qr.CameraReader(None, url_builder=qr.unresolved_camera_builder(door, DOOR))
            door.camera.start()
            try:
                self.assertTrue(rejected.wait(2))
                time.sleep(.1)
                self.assertTrue(door.camera.is_alive())
                relay.assert_not_called()
                decoder.assert_not_called()
            finally:
                door.camera.stop()
                door.camera.join(2)
            self.assertFalse(door.camera.is_alive())

    def test_delayed_discovery_in_dry_run_never_configures_an_actuator(self):
        door = qr.DoorConfig('Synthetic delayed door', None, None, {'synthetic-token'})
        original = copy.deepcopy(DOOR)
        with mock.patch.object(qr, 'discover_onvif_cameras', return_value=[CAMERA]), mock.patch.object(qr, 'OnvifRelayController') as relay:
            builder = qr.unresolved_camera_builder(door, DOOR, dry_run=True)
            self.assertIn(CAMERA['ip'], builder())
            self.assertEqual(door.camera_url, builder())
        relay.assert_not_called()
        self.assertIsNone(door.door)
        self.assertEqual(original, DOOR)
        self.assertEqual({'synthetic-token'}, door.valid_tokens)

    def test_discovery_network_exception_is_retried(self):
        waits = []
        def unavailable(): raise OSError('synthetic interface not ready')
        reader = qr.CameraReader(None, url_builder=unavailable)
        def wait(delay):
            waits.append(delay)
            if len(waits) == 3: reader.stop()
        reader._stop_event.wait = wait
        with mock.patch.object(qr.video_decode, 'open_reader', return_value=None) as decoder:
            reader.run()
        self.assertEqual([2, 5, 10], waits)
        decoder.assert_not_called()


if __name__ == '__main__':
    unittest.main()
