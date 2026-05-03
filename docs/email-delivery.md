# Plan: email QR codes to residents

Today the admin generates a QR code per resident and physically prints/shares
it. We want one click to email it - to one resident or to a selected batch -
with sent-status feedback in real time on the dashboard.

## User-facing surface

### 1. Settings -> Email tab

New tab on `/settings` next to Doors / Integrations. Form:

| Field | Notes |
|---|---|
| SMTP host | e.g. `smtp.sendgrid.net`, `smtp.gmail.com` |
| SMTP port | default 587 (STARTTLS) |
| Username | SMTP auth user |
| Password | SMTP auth password (write-only; saved encrypted) |
| From name | e.g. "Building Manager" |
| From email | must be authorised on the relay |
| Reply-to (optional) | |
| Subject template | e.g. `Your QR access code for {{ unit_label }}` |
| Body template | multi-line, supports `{{ full_name }}`, `{{ unit_label }}`, `{{ building }}` |
| [Send test email] | sends to admin's own email (login email) so they can preview |

Stored in `app_settings` under key `email.config` (same JSON pattern as
`buildium.config`). Password lives encrypted at rest using a new symmetric
key initialized on first save and stored in a sibling row.

Status banner (similar to Buildium's "Connected" pill) shows
"Email configured (smtp.x.com)" + last test result + last queue depth.

### 2. Dashboard - per-user "Email QR code" action

In the existing Actions column, alongside `[QR Codes] [Delete]`:

```
[QR Codes] [Email] [Delete]
```

Clicking `[Email]` POSTs `/api/users/<id>/email-qr`, server enqueues an
email job, response is immediate, status appears in the new Email column
(see below).

Disabled (grey) for users with no `email`.

### 3. Dashboard - mass actions

Above the user table:

```
[ ] Select all   [Email QR code v]   (3 selected)
```

- Each row gets a leading checkbox.
- Header checkbox toggles all visible rows (respects row filters once we add them).
- Dropdown "Mass action" lists `Email QR code` for now; revoke/delete can come later.
- Confirm modal: "Send QR codes to 3 residents?"
- POSTs `/api/users/email-qr-batch` with `{user_ids: [...]}`. Server enqueues
  one job per recipient.

### 4. Dashboard - Email column

New column right of `Phone`:

| Source | Name | Email | Phone | Last sent | ... |
|---|---|---|---|---|---|

`Last sent` shows:
- `-` if never sent
- timestamp + relative ("2 min ago") + tooltip with delivery details when present
- `Sending...` (yellow) while queued/in flight
- `Failed` (red) with hover tooltip showing error message

Updates in real time via SSE (see "Real-time updates" below). No polling.

## Backend

### Schema additions (`web/db.py`)

New table `email_jobs`:

```sql
CREATE TABLE IF NOT EXISTS email_jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_id        INTEGER NOT NULL REFERENCES tokens(id) ON DELETE CASCADE,
    to_email        TEXT NOT NULL,
    subject         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'queued',   -- queued/sending/sent/failed
    error           TEXT,
    queued_at       TEXT NOT NULL DEFAULT (datetime('now')),
    started_at      TEXT,
    finished_at     TEXT
);
CREATE INDEX idx_email_jobs_user_id ON email_jobs(user_id);
CREATE INDEX idx_email_jobs_status  ON email_jobs(status);
```

`users.last_email_sent_at` (denormalised TEXT) - kept current by the
worker. Index for cheap dashboard reads.

`app_settings` rows:
- `email.config` - JSON of SMTP creds + templates (password field stored
  via the same encryption helper Buildium uses).

### Worker

Single-process queue worker thread started by `run.py` next to the
detector:

- Pulls `WHERE status='queued' ORDER BY id LIMIT N` (N = max parallelism, default 4)
- Marks `status='sending', started_at=now()`, broadcasts SSE event
- Sends via `smtplib.SMTP(host, port).starttls().login().send_message(...)`
  with the QR PNG attached (via `MIMEImage`) and the rendered subject/body
- On success: `status='sent', finished_at=now()`, update `users.last_email_sent_at`,
  broadcast SSE
- On failure: retry with exponential backoff up to 3 attempts (5s, 30s, 5min);
  after that mark `status='failed'`, store `error`, broadcast SSE

Rate limit: configurable `max_per_minute` in email.config; default 30 to stay
well under typical SMTP relay throttles. Worker sleeps if it'd exceed.

### Routes

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/integrations/email` | - | `{configured, smtp_host, from_email, last_sent_at}` (password masked) |
| POST | `/api/integrations/email` | `{smtp_host, smtp_port, username, password, ...}` | validates by sending a test email to caller's address |
| POST | `/api/integrations/email/test` | same | doesn't persist, just sends a probe |
| POST | `/api/users/<id>/email-qr` | - | `{job_id, queued_at}` - returns immediately |
| POST | `/api/users/email-qr-batch` | `{user_ids: []}` | `{queued_count}` |
| GET | `/api/email-jobs/stream` | - | **SSE** stream of job state changes |

### Real-time updates (no polling)

Existing `web/events.py` already has an in-process EventBus used for the
access log SSE. Reuse it:

- Worker calls `event_bus.publish('email-job', {job_id, user_id, status, ...})`
  on every state transition.
- Dashboard subscribes via `EventSource('/api/email-jobs/stream')`.
- Each event updates the right row's `Last sent` cell in place. No reload,
  no polling.

## UI/UX details

- **Per-user** button can be a small icon (envelope) to save column width.
- **Mass action** confirm modal shows the recipient count + a sample
  rendered subject for the first user so the admin sees what it looks like.
- **Templates**: live preview pane on the Email settings tab - admin sees
  the rendered subject/body for "their own" user record next to the form.
- **Disabled state** propagates: users without email show greyed-out
  Email button; checkbox is disabled in their row; mass-action count
  excludes them.

## Out of scope for v1

- **SMS delivery** - schema (`phone_primary` already exists) supports it;
  implement via Twilio/Plivo provider once SMTP is solid. Same job-queue
  shape, different worker.
- **Per-user opt-out** / unsubscribe link.
- **HTML email body** (we ship plain-text + attachment first; HTML can layer).
- **Resend / cancel** of failed jobs from the UI - admins can re-trigger
  via the existing Email button.
- **Webhook receivers** for bounce/complaint feedback from the SMTP relay.
- **Quotas / spend limits** beyond the simple per-minute rate.

## Open questions

- Encryption key storage: use the same secret approach as Buildium's
  `client_secret` (currently stored in plain JSON inside `app_settings`).
  Should both move to a real keyring? Tracked separately.
- Default rate limit: 30/min seems safe for any commercial relay; raise
  per-account if the admin's relay tolerates more.

## Phasing

1. **v1**: Email tab + per-user `[Email]` action + SSE-driven status. No
   batch yet, no templates UI - hardcoded subject/body. Get end-to-end
   delivery working.
2. **v2**: Templates UI + live preview, batch + checkboxes + mass-action
   dropdown, retries, rate limiting.
3. **v3 (separate plan)**: SMS delivery via the same queue.
