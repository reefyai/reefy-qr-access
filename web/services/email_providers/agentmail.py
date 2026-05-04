"""AgentMail provider. Hosted-inbox transactional email API.

The provider lazily provisions an inbox on first send and writes the
inbox_id back into app_settings so subsequent sends reuse it. Custom
domains are admin-configured in AgentMail's console - we don't touch
that flow; we just send through whatever address AgentMail stamps on
the inbox.
"""

from __future__ import annotations

import base64
from typing import Any

import requests

from .base import EmailProvider, RateLimitError, PermanentError, TransientError


API_BASE = 'https://api.agentmail.to/v0'
HTTP_TIMEOUT = 30


class AgentMailProvider(EmailProvider):
    def __init__(self, cfg, on_inbox_provisioned=None):
        """on_inbox_provisioned(inbox_id) is called once when we
        auto-create an inbox; the worker passes a callback that
        persists inbox_id back into app_settings."""
        super().__init__(cfg)
        self._inbox_id = cfg.get('agentmail_inbox_id') or None
        self._on_provisioned = on_inbox_provisioned

    def _headers(self) -> dict:
        return {
            'Authorization': f"Bearer {self.cfg['agentmail_api_key']}",
            'Content-Type': 'application/json',
        }

    def _ensure_inbox(self) -> str:
        if self._inbox_id:
            return self._inbox_id
        # Provision a new inbox under @agentmail.to. Free tier supports
        # up to 3 inboxes; admin can rotate via Disconnect if needed.
        resp = self._http('POST', f'{API_BASE}/inboxes', json={})
        body = resp.json() if resp.content else {}
        self._inbox_id = body.get('inbox_id') or body.get('id')
        if not self._inbox_id:
            raise PermanentError(f'inbox provisioning returned no id: {body}')
        if self._on_provisioned:
            try:
                self._on_provisioned(self._inbox_id)
            except Exception:
                pass
        return self._inbox_id

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
        inbox_id = self._ensure_inbox()
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
