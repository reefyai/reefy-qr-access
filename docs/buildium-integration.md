# Plan: Buildium integration (two-table model)

Pull residents (owners + tenants) from a Buildium account so the admin doesn't
add them by hand, and so QR codes can later be sent by email/SMS.

## Why two tables

Imported records live in their own table; `users` keeps a thin link to them.

```
external_users   ←  records imported from any external system. One row per
                    Buildium person today; one row per RealPage / Yardi /
                    AppFolio person tomorrow.

users            ←  qr-access concept. Has tokens, gets QR codes, shown in
                    dashboard. Either manually created (no link) OR backed by
                    one external_users row.
```

Benefits:

- `users` schema doesn't grow each time a new source is added.
- Manual users stay clean (no NULL columns for fields that don't apply).
- Re-syncing only touches `external_users`; `users` updates are derived.
- Full source payload preserved in `raw_json` for debugging without polluting
  the hot path.
- `external_users.is_active_at_source = 0` cascades naturally to inactivate
  the linked user + revoke their tokens.

## How this scales to other sources

Every property-management system has the same universal fields: name, email,
phone, unit, building. Promote those to typed columns. Source-specific
quirks (Buildium's `OccupiesUnit`, `OwnershipAccounts[].DateOfPurchase`,
RealPage's lease metadata, etc.) live in `raw_json`. Adding a new source =
write a small adapter in `web/services/<source>.py` that maps its payload
→ the common columns + dumps the original into `raw_json`. Indexes and
queries stay normal SQL; only feature code that genuinely needs a quirky
field touches JSON.

If a future source's shape is incompatible with the common columns, we split
into per-source tables then. Nothing in this design blocks that.

## Buildium API surface

Two parallel resource trees - both must be crawled or we miss residents.

| Tree | Endpoint |
|---|---|
| Associations | `GET /v1/associations` |
| Association units | `GET /v1/associations/units?associationids={id}` |
| Association owners | `GET /v1/associations/owners?associationids={id}` |
| Association tenants (renters in HOA units) | `GET /v1/associations/tenants?associationids={id}` |
| Rentals | `GET /v1/rentals` |
| Rental units | `GET /v1/rentals/units?propertyids={id}` |
| Active leases | `GET /v1/leases?propertyids={id}&leasestatuses=Active` |
| Lease tenants | `GET /v1/leases/tenants?ids=...` (batched) |

Auth = two HTTP headers per call (`x-buildium-client-id`,
`x-buildium-client-secret`). No OAuth.
Sandbox: `https://apisandbox.buildium.com/v1/`.
Prod: `https://api.buildium.com/v1/`.

Person record (owners + tenants share the same shape):

```json
{
  "Id": 12345,
  "FirstName": "Jane", "LastName": "Doe",
  "Email": "jane@example.com",
  "AlternateEmail": "",
  "PhoneNumbers": [{"Number": "(555) 555-0100", "Type": "Cell"}],
  "PrimaryAddress": {"AddressLine1": "...", "City": "...", "State": "...", "PostalCode": "..."},
  "OwnershipAccounts": [{"AssociationId": 1, "UnitId": 99, "Status": "Active"}],
  "OccupiesUnit": true   // owners only - false = off-site landlord
}
```

Unit lookup gives a human label (e.g. `"Building 3A - Main St"`) used in the
qr-access UI.

## Schema changes (`web/db.py`)

### New table: `external_users`

```sql
CREATE TABLE IF NOT EXISTS external_users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,   -- our internal PK
    source          TEXT    NOT NULL,                    -- 'buildium'
    source_kind     TEXT    NOT NULL,                    -- 'assoc_owner' | 'assoc_tenant' | 'rental_tenant'
    source_id       TEXT    NOT NULL,                    -- source's identifier (Buildium person Id today; UUID tomorrow). Used to re-find on next sync.

    -- Common columns (universal across PM systems)
    first_name      TEXT    NOT NULL DEFAULT '',
    last_name       TEXT    NOT NULL DEFAULT '',
    email           TEXT,
    alternate_email TEXT,
    phone_primary   TEXT,
    phone_secondary TEXT,
    unit_source_id  TEXT,                                -- source-side unit id
    unit_label      TEXT,                                -- e.g. 'Building 3A - Main St'
    building        TEXT,                                -- association/property name

    -- Full source payload, source-specific quirks live here
    raw_json        TEXT    NOT NULL DEFAULT '{}',

    is_active_at_source INTEGER NOT NULL DEFAULT 1,
    first_seen_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    last_synced_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (source, source_kind, source_id)
);
```

`source_id` is TEXT to accept both ints (Buildium) and UUIDs (other sources).

### `users` table additions (idempotent ALTERs in `init_db()`)

| Column | Type | Notes |
|---|---|---|
| `external_user_id` | INTEGER | NULL = manually created. References `external_users(id)`. UNIQUE index. |
| `is_active` | INTEGER NOT NULL DEFAULT 1 | Inactivated when linked external_users row goes inactive. |
| `created_via` | TEXT NOT NULL DEFAULT 'manual' | 'manual' / 'sync:buildium' - audit + UI badge. |

`email` becomes nullable + non-unique. Two residents in the same unit (e.g.
spouses sharing a family inbox) can carry the same address: each gets their
own `users` row, their own `full_name`, their own tokens. Identity is
`(external_users.source, source_kind, source_id)` for synced users, and
`users.id` for everyone. Manual-add path still validates non-empty email at
the route level.

### `tokens` table

No schema change. Sync logic flips `tokens.active = 0` for all tokens of any
user it just inactivated.

### New table: `app_settings`

```sql
CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,                           -- JSON-encoded
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

Holds `buildium.config` (client id / secret / base url) and
`buildium.last_sync` (timestamp + counts). Avoids YAML on a separate volume;
admin edits via UI without restart.

## Sync semantics (idempotent, atomic)

One transaction per sync:

1. **Fetch** from Buildium - associations + rentals trees - into an in-memory
   list of person records, each tagged with
   `(source_kind, source_id, common fields, raw payload)`.
2. **Upsert** into `external_users` keyed on
   `(source, source_kind, source_id)`. Set `is_active_at_source = 1`,
   refresh all common columns + `raw_json`, set `last_synced_at = now()`.
3. **For each new `external_users` row** (no linked user yet): create a
   matching `users` row, set `external_user_id`,
   `created_via = 'sync:buildium'`, `is_active = 1`.
   - `users.full_name` = `first_name + ' ' + last_name`
   - `users.email` mirrored from `external_users.email` (nullable)
   - `users.address` = `unit_label || ', ' || building` for legacy display
4. **Stale handling**: any `external_users` row with this source whose
   `(kind, id)` is *not* in the just-fetched set:
   - `external_users.is_active_at_source = 0`
   - linked `users.is_active = 0`
   - that user's `tokens.active = 0`
   - **Rows kept** (not deleted) so historical access logs stay readable.
5. **Manual users** (`external_user_id IS NULL`): never inactivated by sync.
6. **Reappearance**: an external_users row going from
   `is_active_at_source = 0` back to `1` re-activates the user
   (`users.is_active = 1`); tokens stay revoked - admin re-issues. Safer
   default than auto-reactivating physical access.

Sync returns:

```json
{
  "external_users": {"created": 0, "updated": 0, "inactivated": 0, "reactivated": 0},
  "users":          {"created": 0, "reactivated": 0, "inactivated": 0},
  "tokens_revoked": 0,
  "associations": 0,
  "rentals": 0,
  "duration_s": 0.0,
  "errors": []
}
```

## Reads (rendering the user table)

```sql
SELECT u.id, u.full_name, u.email, u.address, u.is_active, u.created_via,
       eu.unit_label, eu.building, eu.phone_primary, eu.alternate_email
FROM users u
LEFT JOIN external_users eu ON eu.id = u.external_user_id
ORDER BY u.created_at DESC;
```

`created_via` lets the UI badge sync'd users (`Buildium` chip).

## New service file: `web/services/buildium.py`

Two layers:

1. `BuildiumClient(client_id, secret, base_url)`:
   - `list_associations()`, `list_assoc_units(id)`, `list_assoc_owners(id)`, `list_assoc_tenants(id)`
   - `list_rentals()`, `list_rental_units(id)`, `list_active_leases(id)`, `list_lease_tenants(ids)`
   - Pagination: yields 1000-at-a-time, sorted by `Id`, until short page.
   - 429 retry with exponential backoff (per Buildium docs, ~200ms base).
   - All calls timeout-bounded.
2. `run_sync(client) -> dict` orchestrator. Pure function on (Buildium data,
   current DB state) - easy to test.

## Routes (`web/app.py`)

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/integrations/buildium` | - | `{configured, base_url, client_id_tail, last_sync, last_counts}` (secret never returned) |
| POST | `/api/integrations/buildium` | `{client_id, client_secret, base_url}` | `{ok}` - validates creds via `/associations?limit=1` then saves to `app_settings` |
| POST | `/api/integrations/buildium/test` | `{client_id, client_secret, base_url}` | `{ok, association_count}` - lets admin verify creds without saving |
| POST | `/api/integrations/buildium/sync` | - | sync result dict (above) - long-running; runs in request thread |

All gated by the existing `@login_required`. "Save & Sync" = POST config, then
POST sync.

## UI: Integrations section in `settings.html`

Append below the existing Doors / Discovered Devices blocks:

```
Integrations
└─ Buildium
   ┌───────────────────────────────────────────────────────────┐
   │ Base URL: [Production v]                                   │
   │ Client ID: [_______________________________]               │
   │ Secret:    [_______________________________]               │
   │ [Test connection]  [Save & Sync]                           │
   ├───────────────────────────────────────────────────────────┤
   │ Status: configured (client ...XXXX, prod)                  │
   │ Last sync: <timestamp>  · N users · M inactivated          │
   │ [Sync now]                                                 │
   └───────────────────────────────────────────────────────────┘
```

In the user table on `dashboard.html`:
- Buildium-sourced row: small `Buildium` badge next to the name.
- `is_active=0` row: greyed out + `(inactive)` suffix; their tokens already
  render as inactive via existing `tokens.active` styling.
- Hover/expand row reveals `unit_label` + `phone_primary` from the joined
  `external_users`.

## Out of scope for v1 (parking lot)

- **Webhooks** (`Tenant.Updated`, `AssociationOwner.Updated`) for delta sync.
  Full crawl is fast enough today.
- **Auto-issuing QR tokens** on first sync. Admin still clicks "Generate QR"
  per user. Bulk-issue button can come later.
- **Sending QR by email/SMS** - schema stores both emails + both phones, but
  the dispatch path is a separate feature.
- **Multi-tenant qr-access**: assumes one Buildium account per install.
- **Admin-side merge/unlink** of an external_users row to a different user -
  rare; can be added once basic sync is stable.
