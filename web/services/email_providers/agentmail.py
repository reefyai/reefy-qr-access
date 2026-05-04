"""AgentMail provider. Hosted-inbox transactional email API.

The admin pre-provisions an inbox in the AgentMail console (so they
control username, optional custom domain, branding) and pastes the
inbox-scoped API key + the inbox's email address into our Settings.
We never call POST /v0/inboxes - that endpoint is only allowed for
org-scoped keys, and inbox-scoped keys are the common case.

The inbox's email IS the inbox_id in the AgentMail API URL path -
no separate identifier needed.
"""

from __future__ import annotations

import base64
from typing import Any

import requests

from .base import EmailProvider, RateLimitError, PermanentError, TransientError


API_BASE = 'https://api.agentmail.to/v0'
HTTP_TIMEOUT = 30


class AgentMailProvider(EmailProvider):
    def _headers(self) -> dict:
        return {
            'Authorization': f"Bearer {self.cfg['agentmail_api_key']}",
            'Content-Type': 'application/json',
        }

    def _http(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        try:
            r = requests.request(method, url, headers=self._headers(),
                                  timeout=HTTP_TIMEOUT, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as e:
            raise TransientError(f'AgentMail network: {e}') from e
        if r.status_code == 429:
            raise RateLimitError(
                f'AgentMail rate limit: {r.text[:200]}')
        if 500 <= r.status_code < 600:
            raise TransientError(
                f'AgentMail {r.status_code}: {r.text[:200]}')
        if r.status_code == 401 or r.status_code == 403:
            raise PermanentError(
                f'AgentMail auth: {r.text[:200]}')
        if 400 <= r.status_code < 500:
            # Treat 4xx (other than 429/401/403) as permanent: bad
            # recipient, malformed body, etc. Retrying won't help.
            raise PermanentError(
                f'AgentMail {r.status_code}: {r.text[:200]}')
        return r

    def send_one(self, to_email, subject, html, text, qr_png_bytes, reply_to=None):
        inbox_id = self.cfg['from_email']
        body = {
            'to': to_email,
            'subject': subject,
            'html': html,
            'text': text,
            'attachments': [{
                'filename': 'qr.png',
                'content_type': 'image/png',
                'content_disposition': 'inline',
                'content_id': 'qrcode',
                'content': base64.b64encode(qr_png_bytes).decode('ascii'),
            }],
        }
        if reply_to or self.cfg.get('reply_to'):
            body['reply_to'] = reply_to or self.cfg['reply_to']
        self._http('POST', f'{API_BASE}/inboxes/{inbox_id}/messages/send',
                    json=body)
