"""Common shape for email provider backends + classified exceptions
the worker uses to decide retry-vs-fail."""

from __future__ import annotations


class EmailError(Exception):
    """Base for provider send errors. Worker treats unknown subclasses
    as transient (retry with short backoff)."""


class RateLimitError(EmailError):
    """HTTP 429 / SMTP 421 quota exhausted / similar. Worker defers
    with a long backoff (hours) - a tight retry loop just burns
    daily quota."""


class TransientError(EmailError):
    """Network blip, 5xx, timeouts. Worker retries with shorter
    backoff (minutes), gives up after max_attempts."""


class PermanentError(EmailError):
    """Bad address, auth failure, 4xx (other than 429). No point
    retrying - worker marks status='failed' immediately."""


class EmailProvider:
    """Subclass + override send_one. Subclasses must raise the
    appropriate exception type so the worker can classify."""

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def send_one(self, to_email: str, subject: str,
                  html: str, text: str, qr_png_bytes: bytes,
                  reply_to: str | None = None) -> None:
        raise NotImplementedError

    def send_test(self, to_email: str) -> None:
        """Override only if a provider needs special setup for test
        sends. Default implementation reuses send_one with a tiny
        1x1 PNG."""
        import base64
        sample = ('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAA'
                  'AAYAAjCB0C8AAAAASUVORK5CYII=')
        png = base64.b64decode(sample)
        self.send_one(to_email,
                      subject='qr-access email test',
                      html='<p>Email integration configured correctly. This is a test.</p>',
                      text='Email integration configured correctly. This is a test.',
                      qr_png_bytes=png)
