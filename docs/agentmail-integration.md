# Plan: AgentMail as alternative email provider

Today the Email integration is SMTP-only (typically Gmail App Password).
This plan adds [AgentMail](https://www.agentmail.to/) as a second
provider the admin can pick in **Settings -> Email**, sharing the same
queue + worker + dashboard plumbing.

The interesting upside vs SMTP isn't actually the send path - it's
the *receive* path: AgentMail gives each integration its own inbox
plus webhooks for inbound mail, which sets up a future "resident
replies appear in the dashboard" feature without us standing up our
own IMAP poller.

## What AgentMail is, in one paragraph

Transactional email API (POST a JSON body, get an email sent) **plus**
a hosted inbox product (each inbox has an address, receives mail,
emits webhooks for `message.received` / `.bounced` / `.complained`,
also exposes WebSocket and IMAP/SMTP). Pitched at AI agents but works
fine for any service-to-resident messaging. New product (2024-ish);
SOC 2 claimed; deliverability not yet third-party benchmarked.

## Why bother (vs sticking with SMTP)

| | Gmail SMTP (current) | AgentMail |
|---|---|---|
| Auth setup | Enable 2FA, create 16-char App Password, paste | Paste a single Bearer API key |
| From address | Real human Gmail account | Auto-issued `*@agentmail.to` for free, custom domain on $20/mo plan |
| Inbound replies | Land in the human's Gmail; no programmatic feed | Webhook-pushed into qr-access via Svix-signed `POST` |
| Per-tenant isolation | One inbox shared by everyone the admin emails | Per-integration inbox |
| Volume @ free tier | ~500/day | 3,000/mo |
| Deliverability for ~150/mo to a known list | Excellent (mature reputation) | Unknown; @agentmail.to may spam-flag with unfamiliar recipients. Verified custom domain on Developer ($20/mo) tier is the realistic config to match Gmail. |

For the immediate send-the-QR-codes use case, Gmail is fine. **The
real motivation for AgentMail is the inbound webhook**, which makes
a future "resident replied to their QR email - show it in the
dashboard" feature ~5x cheaper to build than rolling our own IMAP.

## What we'd need from the admin

Minimum: paste an `AgentMail API key` (`am_...`) from
https://console.agentmail.to. The qr-access worker will call
`POST /v0/inboxes` once on first send to create a provisioned inbox
under `*@agentmail.to` and store its `inbox_id` in `app_settings`.

If the admin later wants a custom-domain From address
(`qr@yourhoa.com`), they set that up entirely in AgentMail's console
- DNS verification + their paid Developer plan. **No change on our
side**: we send through the same `inbox_id`, AgentMail just stamps
the verified custom address on outbound mail. So this plan covers
both default and custom domains from day one.

## Architecture: provider abstraction in the email service

`web/services/email.py` currently calls `smtplib.SMTP` directly inside
`send_one()`. Refactor:

```
web/services/email_providers/
    __init__.py          - registry: PROVIDERS = {'smtp': SmtpProvider, 'agentmail': AgentMailProvider}
    base.py              - class EmailProvider: send_one(to, subject, html, text, qr_png_bytes)
    smtp.py              - existing smtplib code, lifted into a class
    agentmail.py         - new
```

`web/services/email.py` keeps the queue + worker + DB plumbing; just
calls `PROVIDERS[cfg['provider']].send_one(...)` instead of inlining
SMTP. All existing routes (`/api/integrations/email`, `/test`,
`/users/<id>/email-qr`, batch) keep working as-is.

### `agentmail.py` shape (~30 lines)

```python
import base64
import requests

API_BASE = 'https://api.agentmail.to/v0'

class AgentMailProvider:
    def __init__(self, cfg):
        self.api_key = cfg['agentmail_api_key']
        self.inbox_id = cfg.get('agentmail_inbox_id') or self._provision_inbox()
        self.from_name = cfg.get('from_name', '')

    def _provision_inbox(self):
        # POST /v0/inboxes -> returns {inbox_id, address}
        # Caller persists inbox_id back into app_settings.
        ...

    def send_one(self, to_email, subject, html, text, qr_png_bytes, reply_to=None):
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
                'content': base64.b64encode(qr_png_bytes).decode(),
            }],
        }
        if reply_to:
            body['reply_to'] = reply_to
        r = requests.post(
            f'{API_BASE}/inboxes/{self.inbox_id}/messages/send',
            json=body, timeout=30,
            headers={'Authorization': f'Bearer {self.api_key}'})
        r.raise_for_status()
```

CID-inline attachment is wire-compatible with what we already produce
for SMTP, so the existing default HTML body (`<img src="cid:qrcode">`)
works unchanged.

## Schema additions

`app_settings` JSON under `email.config` gains:

```json
{
  "provider": "smtp",                    // or "agentmail"
  "smtp_host": "...", "username": "...", "password": "...",  // when provider=smtp
  "agentmail_api_key": "am_...",         // when provider=agentmail
  "agentmail_inbox_id": "inb_...",       // populated lazily on first send
  "from_email": "...", "from_name": "...", "reply_to": "...",
  "subject_template": "...", "body_html_template": "...", "body_text_template": "..."
}
```

`from_email` becomes optional when `provider=agentmail` and no custom
domain is configured (the inbox's auto-issued address is used).

## Settings UI changes

Top of the Email tab gets a provider radio:

```
Provider:  ( ) SMTP   ( ) AgentMail
```

Switching toggles which subset of fields shows. SMTP fields stay
exactly as today; AgentMail block is just:

```
API key: [____________________________]            [?]
   ?-tooltip: 'Get one at console.agentmail.to. Free
              plan covers 3,000 emails/month.'

Inbox: (auto-created on first send)
       qr-access-<random>@agentmail.to     [Disconnect]
       (or: <hoa>.agentmail.to once verified custom domain)
```

Templates + Send Test panels are shared across providers - they don't
care how the message goes out.

## Rate limits + persistent retries

AgentMail's free tier caps at **100 messages/day** (in addition to the
3,000/month total). A 120-resident initial blast WILL hit it. The
worker today has no retry path - one HTTP failure = permanent
`status='failed'`. Needs hardening regardless of provider (Gmail
throttles too):

**Schema** - add to `email_jobs`:

```sql
ALTER TABLE email_jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE email_jobs ADD COLUMN next_retry_at TEXT;
```

**Worker query** changes to:

```sql
SELECT ... FROM email_jobs
WHERE status='queued'
  AND (next_retry_at IS NULL OR next_retry_at <= datetime('now'))
ORDER BY id LIMIT 1
```

**On send failure**: classify the exception:
- HTTP 429 / "rate limit" / "quota exceeded": **defer**. Bump
  `attempts`, set `next_retry_at = now + backoff(attempts)` where
  backoff is `[15min, 1h, 4h, 12h, 24h]`. Keep `status='queued'`.
  At ~100/day cap, the queue will naturally drain over the next day
  with zero admin involvement.
- Other transient (connection error, 5xx): **shorter backoff** like
  `[30s, 2min, 10min, 1h]`, max 5 attempts.
- Permanent (4xx other than 429: bad address, auth fail): **fail
  immediately**, no point retrying.

**Survives restart**: `email_jobs` lives in SQLite; the worker's
"pop next queued" query above naturally resumes on container start.
No in-memory state to lose.

**Dashboard surface**: the `Last sent` cell already supports
"Sending..." / "Sent at X" / "Failed" via SSE. Add a
"Retrying in 4h" state for `attempts > 0 AND status = 'queued'` so
admins can see a job is healthily-deferred not silently lost.

This work is **provider-agnostic** - lives in the worker, not in
either provider class. Same retry plumbing benefits SMTP/Gmail
throttling too.

## Inbound (v2 - future, not v1)

AgentMail emits webhooks for inbound mail (`message.received`). v2
will surface resident replies inside the dashboard. Out of scope for
this plan - covered separately when we get there.

For v1: send-only. Replies accumulate in the AgentMail console;
admin reads them there.

## Routes (mostly unchanged)

| Method | Path | Notes |
|---|---|---|
| GET  | `/api/integrations/email` | Returns provider + provider-specific masked status |
| POST | `/api/integrations/email` | Body includes `provider` + the matching subset of fields. Validation: SMTP requires host+user+pass+from; AgentMail requires api_key |
| POST | `/api/integrations/email/test` | Routes to the right provider's `send_one` |
| POST | `/api/users/<id>/email-qr` | Unchanged - worker reads provider from cfg |
| POST | `/api/users/email-qr-batch` | Unchanged |
| GET  | `/api/email-jobs/stream` | Unchanged |

## E2e

**Provider tests** - mirror the SMTP approach: monkey-patch
`requests.post` (or use `responses`) in `tests/e2e/test_email_api.py`
to capture what the AgentMail provider would have sent; assert
`Authorization` header, URL, base64'd attachment, CID. No network.

**Retry-mechanism tests** (new file, provider-agnostic - tests the
worker against a stub provider that throws on demand):

- `test_rate_limit_defers_with_long_backoff` - stub raises
  `RateLimitError` (HTTP 429). Assert: `status='queued'`,
  `attempts=1`, `next_retry_at` ~15min in the future. No new SMTP
  call attempted on the next worker tick (because next_retry_at
  hasn't elapsed).
- `test_transient_error_retries_then_fails_at_max` - stub raises
  `ConnectionError` 6 times. Assert: attempts climbs 1..5, then on
  the 6th `status='failed'` and `error` populated.
- `test_permanent_error_fails_immediately` - stub raises a 4xx
  classified as "bad address". Assert: `attempts=1`,
  `status='failed'`, no retry scheduled.
- `test_worker_picks_up_due_retries_on_simulated_restart` - insert
  a row with `status='queued'`, `attempts=2`, `next_retry_at` set
  10s ago. Worker (started fresh) pops it on its next tick.
- `test_worker_skips_not_yet_due_retries` - same setup but
  `next_retry_at` 1h in the future. Worker query returns nothing;
  the row stays untouched.
- `test_attempts_counter_persists_across_workers` - process_job
  fails once, then a second worker instance picks it up and sees
  `attempts=1` in the row (i.e. counter survives the restart).

## Out of scope for v1

- Inbound replies in the dashboard (separate v2 plan)
- Custom-domain provisioning UI (admins do that in AgentMail's
  console; we just consume the resulting verified address)
- Per-recipient delivery status webhooks (`message.delivered` /
  `.bounced`) flowing back into our `email_jobs` table
- AWS SES, SendGrid, Postmark, etc. as a third/fourth provider -
  trivial once the abstraction exists, but not until someone needs them

## Phasing

1. **v1**: provider abstraction + AgentMail send path + UI radio +
   retry/backoff worker. No inbound. ~350 LOC + ~8 e2e tests. Ship
   as one image bump.
2. **v2**: inbound replies in the dashboard - separate plan when we
   get there.
