"""SMTP provider (Gmail App Password etc.). Lifted from the original
inline implementation in email.py."""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from .base import EmailProvider, RateLimitError, PermanentError, TransientError


# SMTP error code classification. Most relays follow RFC 5321 / 821.
# 4xx = transient (retry), 5xx = permanent (give up). 421 = service
# not available / rate limited; we treat as RateLimit specifically.
# 550 series with "rate" or "quota" in the message also rate-limited.
_RATE_KEYWORDS = ('rate', 'quota', 'too many', 'exceeded', '421', '450')


class SmtpProvider(EmailProvider):
    def _build(self, to_email, subject, html, text, qr_png_bytes, reply_to):
        msg = EmailMessage()
        msg['From'] = formataddr((self.cfg.get('from_name') or '', self.cfg['from_email']))
        msg['To'] = to_email
        msg['Subject'] = subject
        if reply_to or self.cfg.get('reply_to'):
            msg['Reply-To'] = reply_to or self.cfg['reply_to']
        msg.set_content(text)
        msg.add_alternative(html, subtype='html')
        msg.get_payload()[1].add_related(
            qr_png_bytes, 'image', 'png', cid='<qrcode>')
        return msg

    def send_one(self, to_email, subject, html, text, qr_png_bytes, reply_to=None):
        msg = self._build(to_email, subject, html, text, qr_png_bytes, reply_to)
        host = self.cfg['smtp_host']
        port = int(self.cfg.get('smtp_port', 587))
        ctx = ssl.create_default_context()
        try:
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                smtp.starttls(context=ctx)
                smtp.login(self.cfg['username'], self.cfg['password'])
                smtp.send_message(msg)
        except smtplib.SMTPAuthenticationError as e:
            raise PermanentError(f'SMTP auth failed: {e}') from e
        except smtplib.SMTPRecipientsRefused as e:
            raise PermanentError(f'recipient refused: {e}') from e
        except smtplib.SMTPResponseException as e:
            code = e.smtp_code
            text_msg = (e.smtp_error or b'').decode(errors='ignore').lower()
            if code == 421 or any(k in text_msg for k in _RATE_KEYWORDS):
                raise RateLimitError(f'SMTP {code}: {text_msg[:200]}') from e
            if 400 <= code < 500:
                raise TransientError(f'SMTP {code}: {text_msg[:200]}') from e
            raise PermanentError(f'SMTP {code}: {text_msg[:200]}') from e
        except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected,
                smtplib.SMTPHeloError, OSError, TimeoutError) as e:
            raise TransientError(f'SMTP connection: {e}') from e
        except smtplib.SMTPException as e:
            raise TransientError(f'SMTP error: {e}') from e
