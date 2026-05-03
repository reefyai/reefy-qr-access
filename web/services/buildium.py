"""Buildium API client + sync orchestrator.

Pulls residents (owners + tenants) from Buildium's two parallel resource trees
(Associations and Rentals) and syncs them into qr-access via the
external_users + users tables. See PLAN-buildium.md for design.
"""

from __future__ import annotations

import json
import time
from typing import Iterator

import requests

from .. import db


PAGE_SIZE = 100   # Buildium's max varies per endpoint; 100 is universally safe.
REQUEST_TIMEOUT = 30
MAX_429_RETRIES = 4


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class BuildiumError(Exception):
    pass


class BuildiumClient:
    def __init__(self, client_id: str, client_secret: str, base_url: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = base_url.rstrip('/')
        self._session = requests.Session()
        self._session.headers.update({
            'x-buildium-client-id': client_id,
            'x-buildium-client-secret': client_secret,
            'Accept': 'application/json',
        })

    def _get(self, path: str, params: dict | None = None):
        url = f"{self.base_url}{path}"
        for attempt in range(MAX_429_RETRIES):
            r = self._session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if r.status_code == 429:
                # Buildium docs suggest ~200ms; back off exponentially.
                time.sleep(0.2 * (2 ** attempt))
                continue
            if r.status_code == 401:
                raise BuildiumError("Authentication failed (check client id + secret)")
            if not r.ok:
                raise BuildiumError(
                    f"GET {path} returned {r.status_code}: {r.text[:200]}")
            return r.json()
        raise BuildiumError(f"GET {path} kept hitting 429 after {MAX_429_RETRIES} retries")

    def _paginate(self, path: str, params: dict | None = None) -> Iterator[dict]:
        """Yield items from a list endpoint, paging by offset/limit + Id sort."""
        offset = 0
        params = dict(params or {})
        params.setdefault('limit', PAGE_SIZE)
        params.setdefault('orderby', 'Id')
        while True:
            params['offset'] = offset
            page = self._get(path, params)
            if not page:
                return
            for item in page:
                yield item
            if len(page) < params['limit']:
                return
            offset += params['limit']

    # --- Associations tree ---

    def list_associations(self):
        return list(self._paginate('/associations'))

    def list_assoc_units(self, assoc_id):
        return list(self._paginate(
            '/associations/units', {'associationids': assoc_id}))

    def list_assoc_owners(self, assoc_id):
        return list(self._paginate(
            '/associations/owners', {'associationids': assoc_id}))

    def list_assoc_tenants(self, assoc_id):
        return list(self._paginate(
            '/associations/tenants', {'associationids': assoc_id}))

    # --- Rentals tree ---

    def list_rentals(self):
        return list(self._paginate('/rentals'))

    def list_rental_units(self, property_id):
        return list(self._paginate(
            '/rentals/units', {'propertyids': property_id}))

    def list_active_leases(self, property_id):
        return list(self._paginate(
            '/leases', {'propertyids': property_id, 'leasestatuses': 'Active'}))

    def list_lease_tenants(self, ids):
        """Resolve tenant Ids in batches (Buildium accepts comma-joined `ids`)."""
        if not ids:
            return []
        out = []
        for i in range(0, len(ids), 50):
            chunk = ids[i:i + 50]
            out.extend(self._paginate(
                '/leases/tenants', {'ids': ','.join(str(x) for x in chunk)}))
        return out

    # --- Smoke test for the "Test connection" UI ---

    def test_connection(self):
        result = self._get('/associations', {'limit': 1})
        return {'ok': True, 'sample_count': len(result)}


# ---------------------------------------------------------------------------
# Adapter: Buildium person record -> common columns
# ---------------------------------------------------------------------------

def _phones(person):
    nums = [p.get('Number', '').strip()
            for p in (person.get('PhoneNumbers') or [])
            if p.get('Number')]
    return (nums[0] if nums else None,
            nums[1] if len(nums) > 1 else None)


def _unit_id(person):
    accounts = person.get('OwnershipAccounts') or []
    if not accounts:
        return None
    # First active account preferred; fall back to first.
    for acc in accounts:
        if acc.get('Status') == 'Active':
            return acc.get('UnitId')
    return accounts[0].get('UnitId')


# Allowlist of Buildium fields kept in external_users.raw_json. Anything
# not in here is dropped at sync time, so we never persist sensitive
# fields like TaxId (SSN/EIN), DelinquencyStatus, or EmergencyContact.
# Minimum-necessary principle: keep only what serves a current or
# planned qr-access feature; everything else is leak surface.
RAW_JSON_PERSON_KEYS = {
    'Id', 'FirstName', 'LastName',
    'Email', 'AlternateEmail', 'PhoneNumbers',
    'OccupiesUnit',
    'CreatedDateTime',
    'MoveInDate', 'MoveOutDate',  # tenant-only, useful for "active" filtering
    'Vehicles',                    # kept for upcoming vehicle-access feature
}
RAW_JSON_OWNERSHIP_KEYS = {
    'AssociationId', 'UnitId', 'Status', 'AssociationOwnerIds',
}


def scrub_buildium_payload(person: dict) -> dict:
    """Return a copy of `person` with only the allowlisted fields.

    Drops sensitive/unused fields like TaxId, DelinquencyStatus,
    EmergencyContact, BoardMemberTerms, PrimaryAddress, AlternateAddress,
    MailingPreference, plus per-OwnershipAccount: Comments, DateOfPurchase,
    DateOfSale, DelinquencyStatus.

    Idempotent - running on already-scrubbed data is a no-op.
    """
    out = {k: person[k] for k in RAW_JSON_PERSON_KEYS if k in person}
    accounts = person.get('OwnershipAccounts') or []
    if accounts:
        out['OwnershipAccounts'] = [
            {k: a[k] for k in RAW_JSON_OWNERSHIP_KEYS if k in a}
            for a in accounts
        ]
    return out


def _person_to_common_fields(person, unit_label_by_id, building_name):
    unit_id = _unit_id(person)
    phone1, phone2 = _phones(person)
    return {
        'first_name': person.get('FirstName') or '',
        'last_name': person.get('LastName') or '',
        'email': (person.get('Email') or '').strip() or None,
        'alternate_email': (person.get('AlternateEmail') or '').strip() or None,
        'phone_primary': phone1,
        'phone_secondary': phone2,
        'unit_source_id': str(unit_id) if unit_id else None,
        'unit_label': unit_label_by_id.get(unit_id) if unit_id else None,
        'building': building_name,
        'raw_json': json.dumps(scrub_buildium_payload(person), default=str),
    }


def _full_name(fields):
    name = (fields['first_name'] + ' ' + fields['last_name']).strip()
    return name or '(unnamed)'


def _user_address(fields):
    parts = [p for p in (fields.get('unit_label'), fields.get('building')) if p]
    return ', '.join(parts)


# ---------------------------------------------------------------------------
# Sync orchestrator
# ---------------------------------------------------------------------------

def run_sync(client: BuildiumClient) -> dict:
    """Pull residents from Buildium, upsert into external_users, derive users.

    Atomic: the entire sync runs in one SQLite transaction. Any error rolls
    back. Returns the counts dict described in PLAN-buildium.md.
    """
    started = time.monotonic()
    counts = {
        'external_users': {'created': 0, 'updated': 0,
                           'inactivated': 0, 'reactivated': 0},
        'users': {'created': 0, 'reactivated': 0, 'inactivated': 0},
        'tokens_issued': 0,
        'tokens_revoked': 0,
        'associations': 0,
        'rentals': 0,
        'duration_s': 0.0,
        'errors': [],
    }

    fetched = []  # list of (source_kind, source_id, fields)

    # --- Associations tree ---
    try:
        for assoc in client.list_associations():
            counts['associations'] += 1
            assoc_id = assoc['Id']
            building = assoc.get('Name') or f"Association {assoc_id}"

            units = client.list_assoc_units(assoc_id)
            unit_label_by_id = {
                u['Id']: u.get('UnitNumber') or f"Unit {u['Id']}" for u in units
            }

            for owner in client.list_assoc_owners(assoc_id):
                fields = _person_to_common_fields(owner, unit_label_by_id, building)
                fetched.append(('assoc_owner', owner['Id'], fields))
            for tenant in client.list_assoc_tenants(assoc_id):
                fields = _person_to_common_fields(tenant, unit_label_by_id, building)
                fetched.append(('assoc_tenant', tenant['Id'], fields))
    except BuildiumError as e:
        counts['errors'].append(f"associations: {e}")

    # --- Rentals tree ---
    try:
        for prop in client.list_rentals():
            counts['rentals'] += 1
            prop_id = prop['Id']
            building = prop.get('Name') or f"Rental {prop_id}"

            units = client.list_rental_units(prop_id)
            unit_label_by_id = {
                u['Id']: u.get('UnitNumber') or f"Unit {u['Id']}" for u in units
            }

            tenant_ids = []
            for lease in client.list_active_leases(prop_id):
                for t in (lease.get('CurrentTenants') or []):
                    if t.get('Id'):
                        tenant_ids.append(t['Id'])
            tenants = client.list_lease_tenants(list(set(tenant_ids)))
            for tenant in tenants:
                fields = _person_to_common_fields(tenant, unit_label_by_id, building)
                fetched.append(('rental_tenant', tenant['Id'], fields))
    except BuildiumError as e:
        counts['errors'].append(f"rentals: {e}")

    # --- Apply to DB in one transaction ---
    seen_keys = set()  # (source_kind, source_id) tuples
    fetched_external_ids = set()  # external_users.id values

    conn = db.get_db()
    try:
        for source_kind, source_id, fields in fetched:
            ext_id, created, reactivated = db.upsert_external_user(
                'buildium', source_kind, source_id, fields)
            seen_keys.add((source_kind, str(source_id)))
            fetched_external_ids.add(ext_id)
            if created:
                counts['external_users']['created'] += 1
            else:
                counts['external_users']['updated'] += 1
                if reactivated:
                    counts['external_users']['reactivated'] += 1

            from .users import issue_initial_token
            user = db.find_user_by_external(ext_id)
            if user is None:
                user_id = db.create_user_from_external(
                    external_user_id=ext_id,
                    full_name=_full_name(fields),
                    email=fields['email'],
                    address=_user_address(fields),
                    created_via='sync:buildium')
                counts['users']['created'] += 1
            else:
                user_id = user['id']
                if reactivated and user.get('is_active') == 0:
                    db.set_user_active(user_id, True)
                    counts['users']['reactivated'] += 1

            # Issue a token if the user has none. Covers both the
            # newly-created case AND the backfill case for users imported
            # before the auto-token feature existed. Admin-revoked tokens
            # leave a row with active=0 in the table - those users still
            # have len(tokens) >= 1, so this skip preserves manual revokes.
            if not db.get_user_tokens(user_id):
                comment = ('auto-created via Buildium sync'
                            if user is None
                            else 'backfilled via Buildium sync')
                issue_initial_token(user_id, comment=comment)
                counts['tokens_issued'] += 1

        # Inactivate stale: external_users we didn't see this run
        stale_rows = conn.execute("""
            SELECT eu.id, u.id AS user_id
            FROM external_users eu
            LEFT JOIN users u ON u.external_user_id = eu.id
            WHERE eu.source = 'buildium' AND eu.is_active_at_source = 1
        """).fetchall()
        for r in stale_rows:
            if r['id'] in fetched_external_ids:
                continue
            conn.execute(
                "UPDATE external_users SET is_active_at_source=0, "
                "last_synced_at=datetime('now') WHERE id=?", (r['id'],))
            counts['external_users']['inactivated'] += 1
            if r['user_id']:
                db.set_user_active(r['user_id'], False)
                counts['users']['inactivated'] += 1
                counts['tokens_revoked'] += db.revoke_user_tokens(r['user_id'])

        db.commit()
    except Exception as e:
        db.rollback()
        counts['errors'].append(f"db: {e}")

    counts['duration_s'] = round(time.monotonic() - started, 2)
    return counts
