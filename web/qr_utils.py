"""QR code token generation and PNG creation."""

import os
import secrets
import qrcode
from pathlib import Path

QR_DIR = os.path.join(os.environ.get('QR_CONFIG_DIR', 'config'), 'qr-codes')


def create_token(nbytes=16):
    return secrets.token_hex(nbytes)


def generate_qr_png(token, output_dir=QR_DIR, payload=None):
    """Render the QR for `token`. If `payload` is given, encode that
    string in the QR (typically the user's `short_id` alias - shorter
    text means fewer modules at QR Version 1, ~1.6x bigger modules at
    the same printed size). Filename stays keyed by `token` so the
    URL / lookup contract for callers does not change. We also force
    error correction H so up to 30% of the QR can be damaged - useful
    for the low-bitrate camera-decode use case."""
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    path = Path(output_dir) / f"{token}.png"
    if not path.exists():
        content = payload if payload else token
        qr = qrcode.QRCode(
            version=None,  # auto-fit smallest version
            error_correction=qrcode.constants.ERROR_CORRECT_H,
            box_size=10,
            border=4,
        )
        qr.add_data(content)
        qr.make(fit=True)
        img = qr.make_image(fill_color='black', back_color='white')
        img.save(str(path))
    return path


def get_qr_path(token, output_dir=QR_DIR):
    return Path(output_dir) / f"{token}.png"
