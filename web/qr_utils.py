"""QR code token generation and PNG creation."""

import os
import secrets
import qrcode
from pathlib import Path

QR_DIR = os.path.join(os.environ.get('QR_CONFIG_DIR', 'config'), 'qr-codes')


def create_token(nbytes=16):
    return secrets.token_hex(nbytes)


def generate_qr_png(token, output_dir=QR_DIR):
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    path = Path(output_dir) / f"{token}.png"
    if not path.exists():
        img = qrcode.make(token)
        img.save(str(path))
    return path


def get_qr_path(token, output_dir=QR_DIR):
    return Path(output_dir) / f"{token}.png"
