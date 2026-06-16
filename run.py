#!/usr/bin/env python3
"""
Run both the web UI and the QR detector/door opener in a single process.
Web UI runs in a background thread, detector runs in the main thread.
"""

import os
import sys
import time
import signal
import threading
import argparse
import collections
import queue

# Shared event: web UI sets this when config changes, detector reloads
reload_event = threading.Event()


class LogCapture:
    """Tee stdout/stderr to a ring buffer + SSE subscribers.
    Captures ALL output including dependency libraries (OpenCV, YOLO, etc.)."""

    def __init__(self, original, maxlines=2000):
        self._original = original
        self._buffer = collections.deque(maxlen=maxlines)
        self._lock = threading.Lock()
        self._subscribers = []
        self._buf = ''

    def write(self, text):
        self._original.write(text)
        self._buf += text
        while '\n' in self._buf:
            line, self._buf = self._buf.split('\n', 1)
            line = line.rstrip()
            if not line:
                continue
            with self._lock:
                self._buffer.append(line)
                dead = []
                for q in self._subscribers:
                    try:
                        q.put_nowait(line)
                    except queue.Full:
                        dead.append(q)
                for q in dead:
                    self._subscribers.remove(q)

    def flush(self):
        self._original.flush()

    def fileno(self):
        return self._original.fileno()

    def isatty(self):
        return self._original.isatty()

    def get_lines(self, n=200):
        with self._lock:
            return list(self._buffer)[-n:]

    def subscribe(self):
        q = queue.Queue(maxsize=500)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass


# Install log capture BEFORE any imports that print
log_capture = LogCapture(sys.stdout)
err_capture = LogCapture(sys.stderr)
sys.stdout = log_capture
sys.stderr = err_capture


def run_web(port, password):
    """Start Flask web UI in a background thread."""
    os.environ['QR_ADMIN_PASSWORD'] = password
    os.environ['QR_WEB_PORT'] = str(port)

    from web.app import app, db as web_db, set_reload_event, set_log_capture
    web_db.init_db()
    set_reload_event(reload_event)
    set_log_capture(log_capture, err_capture)

    # Start the email-delivery background worker. Idempotent.
    from web.services import email as email_svc
    email_svc.start_worker()

    # Start the door-health monitor. Idempotent. Does its own work
    # (alarm emails) only when enabled in Settings -> Monitoring.
    from web.services import monitor as monitor_svc
    monitor_svc.start_worker()

    app.run(host='0.0.0.0', port=port, debug=False,
            use_reloader=False, threaded=True)


def run_detector(config_path, skip, conf, model_size, decode_backend,
                 pipeline_backend):
    """Start the QR detector/door opener. Restarts on reload_event."""
    import qr_live

    while True:
        reload_event.clear()

        # Wait for config file to exist (web UI may need to create it)
        if not os.path.exists(config_path):
            print(f"[INFO] Waiting for config file: {config_path}")
            print(f"[INFO] Create doors in the web UI, detector will start automatically")
            while not os.path.exists(config_path):
                if reload_event.wait(timeout=5):
                    reload_event.clear()
                    if os.path.exists(config_path):
                        break
            print(f"[INFO] Config file found: {config_path}")

        # Run detector (blocks until _running=False or reload_event is set)
        sys.argv = [
            'qr_live.py',
            '--config', config_path,
            '--skip', str(skip),
            '--conf', str(conf),
            '--model-size', model_size,
            '--decode-backend', decode_backend,
            '--pipeline-backend', pipeline_backend,
        ]

        qr_live.set_reload_event(reload_event)
        print(f"[INFO] Starting detector...")
        qr_live.main()

        if not reload_event.is_set():
            break  # Normal shutdown (SIGINT/SIGTERM), don't restart

        print(f"[INFO] Config changed, reloading detector...")
        # Reset qr_live state for re-import
        qr_live._running = True
        time.sleep(1)


def main():
    parser = argparse.ArgumentParser(
        description='QR Access - Web UI + Detector')
    parser.add_argument('--web-port', type=int,
                        default=int(os.environ.get('APP_PORT_8080', '8080')),
                        help='Web UI port (default: 8080 or APP_PORT_8080 env)')
    parser.add_argument('--admin-password', default='',
                        help='Admin password (default: from QR_ADMIN_PASSWORD env)')
    parser.add_argument('--config', default='config/doors.yaml',
                        help='Door config YAML path (default: config/doors.yaml)')
    parser.add_argument('--skip', type=int, default=3,
                        help='Process every Nth frame (default: 3)')
    parser.add_argument('--conf', type=float, default=0.3,
                        help='YOLO confidence threshold (default: 0.3)')
    parser.add_argument('--model-size', default=os.environ.get('MODEL_SIZE', 'auto'),
                        choices=['auto', 'n', 's', 'm', 'l'],
                        help='YOLO model size; env MODEL_SIZE overrides. '
                             'auto = n on CPU-only hosts (~2x faster, detects '
                             'QRs fine), s when a GPU is present (full accuracy)')
    parser.add_argument('--pipeline-backend',
                        default=os.environ.get('PIPELINE_BACKEND', 'auto'),
                        choices=['auto', 'cpu', 'igpu', 'gpu'],
                        help='Decode+detect backend; env PIPELINE_BACKEND '
                             'overrides. auto = gpu on NVIDIA, igpu on an '
                             'Intel/AMD iGPU, else cpu (software).')
    parser.add_argument('--decode-backend',
                        default=os.environ.get('DECODE_BACKEND', 'auto'),
                        choices=['auto', 'nvdec', 'vaapi', 'cpu'],
                        help='Override the video decode backend; auto = '
                             'derive from --pipeline-backend.')
    parser.add_argument('--web-only', action='store_true',
                        help='Run only the web UI, no detector')
    args = parser.parse_args()

    password = args.admin_password or os.environ.get('QR_ADMIN_PASSWORD', '')

    print(f"[INFO] === QR Access System ===")
    print(f"[INFO] Web UI:    http://0.0.0.0:{args.web_port}")
    print(f"[INFO] Auth:      {'enabled' if password else 'disabled'}")
    print(f"[INFO] Config:    {args.config}")
    if not args.web_only:
        print(f"[INFO] Detector:  skip={args.skip}, conf={args.conf}, "
              f"model={args.model_size}, pipeline={args.pipeline_backend}, "
              f"decode={args.decode_backend}")

    # Start web UI in background thread
    web_thread = threading.Thread(
        target=run_web, args=(args.web_port, password), daemon=True)
    web_thread.start()
    print(f"[INFO] Web UI started on port {args.web_port}")

    if args.web_only:
        print(f"[INFO] Web-only mode, detector not started")
        signal.signal(signal.SIGINT, lambda s, f: sys.exit(0))
        signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))
        while True:
            time.sleep(1)
    else:
        # Give web UI a moment to start
        time.sleep(2)
        # Run detector in main thread (handles signals)
        run_detector(args.config, args.skip, args.conf, args.model_size,
                     args.decode_backend, args.pipeline_backend)


if __name__ == '__main__':
    main()
