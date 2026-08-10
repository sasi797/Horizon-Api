"""Server-side Indigo (NPA) AddJob integration.

Ported from the field mapping in Horizon-Web's
`src/app/dashboard/manifests/[id]/page.tsx` (`buildIndigoAddJobPayload`) and
`src/lib/hawbFormat.ts` — see Horizon-Web's
`docs/indigo-addjob-integration.md` for the full field-by-field reference.

This runs server-side (not from the browser) because Indigo's API has no
CORS support, so a direct browser call is blocked outright, and because the
account credentials must never ship in the frontend bundle.
"""

import base64
import json
import logging
import re
import uuid
from datetime import datetime

import httpx

from app.core.config import settings
from app.models.hawb import HawbJob, HawbManifest

logger = logging.getLogger(__name__)

UK_POSTCODE_RE = re.compile(r"[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b", re.IGNORECASE)
EIRCODE_RE = re.compile(r"[A-Z]\d{2}\s?[A-Z0-9]{4}\b", re.IGNORECASE)
NUMERIC_POSTCODE_RE = re.compile(r"\b\d{4,6}\b")


def _lines(value: str | None) -> list[str]:
    if not value:
        return []
    return [line.strip() for line in value.split("\n") if line.strip()]


def split_address(value: str | None) -> dict:
    lines = _lines(value)
    if not lines:
        return {"name": "—", "address": ""}
    return {"name": lines[0], "address": ", ".join(lines[1:])}


def city_line(value: str | None) -> str:
    lines = _lines(value)
    return lines[-1] if lines else "—"


def address_country(value: str | None) -> str:
    line = city_line(value)
    return "" if line == "—" else line


# The "Town, Postcode" line reliably sits second-to-last, right before the
# country line — search from the end backward, skipping the first line
# (company/name), so a street number earlier in the address never gets
# mistaken for the postcode.
def city_and_postcode_line(value: str | None) -> dict:
    lines = _lines(value)
    for line in reversed(lines[1:]):
        match = UK_POSTCODE_RE.search(line) or EIRCODE_RE.search(line) or NUMERIC_POSTCODE_RE.search(line)
        if match:
            postcode = match.group(0).upper()
            before = line[: match.start()].strip().rstrip(",").strip()
            town = before.split(",")[0].strip()
            return {"town": town, "postcode": postcode}
    return {"town": "", "postcode": ""}


def to_indigo_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    # collection_at/delivery_at are wall-clock times lifted straight from the
    # HAWB PDF and stored with a fake UTC tag purely to fit a timestamptz
    # column — not real UTC instants. strftime formats the stored digits
    # as-is (no timezone conversion), matching the frontend's approach of
    # reading the ISO string's digits directly.
    return value.strftime("%Y-%m-%dT%H:%M:%S.000")


def _normalize_identity(value: str) -> str:
    return re.sub(r"[.,]+$", "", re.sub(r"\s+", " ", value.lower())).strip()


def address_identity_key(value: str | None) -> str | None:
    """Same-location identity, mirroring Horizon-Web's `addressIdentityKey` in
    `src/lib/hawbFormat.ts`. Deliberately coarser than an exact string match:
    keyed on the company name and the final country/city line only, because the
    middle lines (floor, suite, punctuation) drift between HAWBs OCR'd from
    different source documents even when it's plainly the same building."""
    lines = _lines(value)
    if not lines:
        return None
    name = _normalize_identity(lines[0])
    last = _normalize_identity(lines[-1])
    if not name or name == "—":
        return None
    return f"{name}|{last}"


def stop_address(job: HawbJob) -> str | None:
    """The address the driver physically visits for this HAWB's leg. A
    collection happens at the shipper, a delivery at the consignee — the other
    end of the route is handled by air freight and is never a stop on this run.
    Unset Del/Coll has no answer; export rejects those before it gets here."""
    if job.job_service_type == "collection":
        return job.shipper
    if job.job_service_type == "delivery":
        return job.consignee
    return None


def _contact_key(value: str | None) -> str:
    """Mirrors the frontend's shipper-contact normalization exactly: trim,
    lowercase, collapse internal whitespace (no punctuation stripping)."""
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def group_jobs_by_merge(jobs: list[HawbJob]) -> list[list[HawbJob]]:
    """The manifest's merged stops, exactly as the "Merge" run-order view shows
    them: one group per row on screen, in the same order (`jobs` arrives ordered
    by manifest_sequence, and groups come out ordered by first member). What the
    user merged and reordered on the manifest is what gets booked, so this has
    to stay in lockstep with Horizon-Web's `routeGroups` in
    `src/app/dashboard/manifests/[id]/page.tsx` — if that rule changes, this one
    changes with it.

    Two-tier priority, matching that view: (1) same delivery ("To") address;
    (2) among whatever is left ungrouped, the same named shipper contact. A key
    only one HAWB matched isn't a merge — that HAWB stays a stop of its own.

    Note this groups on the paperwork, not on geography: it keys on the "To"
    without regard to which end of the route the driver actually visits, so a
    group's members can disagree about where the stop is. `validate_merge_groups`
    rejects those before they reach Indigo."""
    to_buckets: dict[str, list[HawbJob]] = {}
    for job in jobs:
        identity = address_identity_key(job.consignee)
        if identity:
            to_buckets.setdefault(identity, []).append(job)

    key_for_job: dict[uuid.UUID, str] = {}
    for identity, bucket in to_buckets.items():
        if len(bucket) < 2:
            continue
        for job in bucket:
            key_for_job[job.id] = f"to:{identity}"

    contact_buckets: dict[str, list[HawbJob]] = {}
    for job in jobs:
        if job.id in key_for_job:
            continue
        contact = _contact_key(job.shipper_contact)
        if contact:
            contact_buckets.setdefault(contact, []).append(job)
    for contact, bucket in contact_buckets.items():
        if len(bucket) < 2:
            continue
        for job in bucket:
            key_for_job[job.id] = f"contact:{contact}"

    groups: dict[str, list[HawbJob]] = {}
    for job in jobs:
        groups.setdefault(key_for_job.get(job.id) or f"single:{job.id}", []).append(job)
    return list(groups.values())


def validate_merge_groups(job_groups: list[list[HawbJob]]) -> list[str]:
    """Every HAWB merged into one drop has to send the driver to one place, so a
    group is only bookable when its members agree on both the Del/Coll leg and
    the address that leg stops at.

    Neither is guaranteed by the merge rule itself: it keys on the "To", so a
    group whose members are collections is keyed on an address nobody visits,
    and its pickups can sit in different countries. Left unchecked the drop
    silently takes whichever HAWB sorted first and the rest vanish from the
    payload — a driver sent to California for a run out of St. Mary's. Returns
    one plain-language problem per unbookable group."""
    problems: list[str] = []
    for group in job_groups:
        if len(group) < 2:
            continue
        hawbs = ", ".join(j.hawb_number for j in group)
        if len({j.job_service_type for j in group}) > 1:
            problems.append(
                f"{hawbs} are merged into one stop but have different Del/Coll "
                "services — a merged stop is a single visit, so they must match."
            )
            continue
        if len({address_identity_key(stop_address(j)) for j in group}) > 1:
            places = " / ".join(dict.fromkeys(
                split_address(stop_address(j))["name"] for j in group
            ))
            leg = "collected from" if group[0].job_service_type == "collection" else "delivered to"
            problems.append(
                f"{hawbs} are merged into one stop but are {leg} different "
                f"addresses ({places}) — a merged stop is a single visit, so they "
                "must be the same place."
            )
    return problems


def _matching_contact(address: str | None, jobs: list[HawbJob]) -> tuple[str, str]:
    """Start point / End point are picked from one of this manifest's own HAWB
    addresses (the exact same text as that job's shipper/consignee) — so the
    contact/phone for the top-level Col/Del can be borrowed from whichever
    HAWB that address actually came from, rather than left blank."""
    if not address:
        return "", ""
    for job in jobs:
        if job.shipper == address:
            return job.shipper_contact or "", job.shipper_phone or ""
        if job.consignee == address:
            return job.consignee_contact or "", job.consignee_phone or ""
    return "", ""


def build_indigo_addjob_payload(
    service_type: str,
    manifest: HawbManifest,
    job_groups: list[list[HawbJob]],
) -> dict:
    """One manifest books as exactly one Indigo Job: the top-level Col/Del is
    the manifest's own Start point / End point (the vehicle's overall route),
    and every HAWB stop in between — one per group from `group_jobs_by_merge` —
    rides along as an AdditionalDrops entry, Collection or Delivery per that
    group's shared job_service_type."""
    col = city_and_postcode_line(manifest.start_point)
    dele = city_and_postcode_line(manifest.end_point)
    col_split = split_address(manifest.start_point)
    del_split = split_address(manifest.end_point)

    all_jobs = [job for group in job_groups for job in group]
    total_packs = sum(j.package_qty or 0 for j in all_jobs)
    total_weight = sum(float(j.weight_kg or 0) for j in all_jobs)
    special_insts = " — ".join(dict.fromkeys(j.special_handling for j in all_jobs if j.special_handling))
    col_contact, col_phone = _matching_contact(manifest.start_point, all_jobs)
    del_contact, del_phone = _matching_contact(manifest.end_point, all_jobs)

    drops = []
    for drop_no, group in enumerate(job_groups, start=1):
        job = group[0]
        is_collection = job.job_service_type == "collection"
        # Reading the leg and the address off the first member is shorthand, not
        # a pick between disagreeing values: validate_merge_groups has already
        # rejected any group whose members differ on either.
        address = job.shipper if is_collection else job.consignee
        addr_split = split_address(address)
        addr_line = city_and_postcode_line(address)
        # The times, though, genuinely can differ across a merged stop: the
        # driver makes one visit and has to satisfy the tightest of them, so
        # the drop carries the earliest rather than whichever HAWB sorted first.
        stop_times = [j.collection_at if is_collection else j.delivery_at for j in group]
        stop_time = min((t for t in stop_times if t is not None), default=None)
        drops.append({
            "DropNo": str(drop_no),
            "Type": "Collection" if is_collection else "Delivery",
            "DateTime": to_indigo_datetime(stop_time),
            "Contact": (job.shipper_contact if is_collection else job.consignee_contact) or "",
            "Company": addr_split["name"],
            "Address1": addr_split["address"],
            "Address2": "",
            "Address3": "",
            "Town": addr_line["town"],
            "Postcode": addr_line["postcode"],
            "Country": address_country(address),
            "Telephone": (job.shipper_phone if is_collection else job.consignee_phone) or "",
            "Insts": "",
            "ReadyAt": "",
            "PremisesClose": "",
            "Packs": sum(j.package_qty or 0 for j in group),
            "Weight": sum(float(j.weight_kg or 0) for j in group),
        })

    # A collection-only manifest has nowhere for the goods to land: every stop
    # is a pick-up and no HAWB carries a UK delivery leg, so the run has to end
    # with one final drop-off of everything collected. That stop is the
    # manifest's own End point, appended as drop n+1 carrying the manifest
    # totals. A manifest that already has a delivery leg is left alone — its
    # goods are dropped at that HAWB's own consignee.
    if drops and all(drop["Type"] == "Collection" for drop in drops):
        drops.append({
            "DropNo": str(len(drops) + 1),
            "Type": "Delivery",
            "DateTime": "",
            "Contact": del_contact,
            "Company": del_split["name"],
            "Address1": del_split["address"],
            "Address2": "",
            "Address3": "",
            "Town": dele["town"],
            "Postcode": dele["postcode"],
            "Country": address_country(manifest.end_point),
            "Telephone": del_phone,
            "Insts": "",
            "ReadyAt": "",
            "PremisesClose": "",
            "Packs": total_packs,
            "Weight": total_weight,
        })

    job_payload = {
        "JobGuid": uuid.uuid4().hex,
        "CustomerNumber": manifest.account_number or "",
        "ServiceType": service_type,
        "VehicleType": manifest.vehicle_size or "",
        "JobReference": manifest.job_reference or "",
        "JobReference2": "",
        "BookedBy": "",
        "RequestedBy": "Horizon Web",
        "ColDateTime": "",
        "ColCompany": col_split["name"],
        "ColContact": col_contact,
        "ColAddress1": col_split["address"],
        "ColAddress2": "",
        "ColAddress3": "",
        "ColTown": col["town"],
        "ColPostcode": col["postcode"],
        "ColCountry": address_country(manifest.start_point),
        "ColTelephone": col_phone,
        "ColEmail": "",
        "ColInsts": "",
        "ColReadyAt": "",
        "ColPremisesClose": "",
        "DelDateTime": "",
        "DelCompany": del_split["name"],
        "DelContact": del_contact,
        "DelAddress1": del_split["address"],
        "DelAddress2": "",
        "DelAddress3": "",
        "DelTown": dele["town"],
        "DelPostcode": dele["postcode"],
        "DelCountry": address_country(manifest.end_point),
        "DelTelephone": del_phone,
        "DelInsts": "",
        "DelReadyAt": "",
        "DelPremisesClose": "",
        "AdditionalDrops": drops,
        "Packs": total_packs,
        "Weight": total_weight,
        "SpecialInsts": special_insts,
        "Length": 0,
        "Width": 0,
        "Height": 0,
        "Fragile": 0,
        "Security": 0,
        "ConsignmentNo": manifest.job_reference or "",
        "Insurance": 0,
        "InsuranceValue": 0,
    }

    return {"Jobs": {"Job": [job_payload]}}


class IndigoRequestError(Exception):
    pass


def account_credentials(account_number: str | None) -> dict:
    """Which NPA instance and login this manifest books against, chosen by its
    account number (settings.INDIGO_ACCOUNTS). An unconfigured account is a hard
    error rather than a fallback — booking a real job against whichever account
    happened to be the default is not a mistake worth being lenient about."""
    account = (account_number or "").strip()
    creds = settings.INDIGO_ACCOUNTS.get(account)
    if not creds:
        known = ", ".join(sorted(settings.INDIGO_ACCOUNTS)) or "none"
        raise IndigoRequestError(
            f"No Indigo credentials are configured for account number '{account}'. "
            f"Configured accounts: {known}."
        )
    return creds


async def call_indigo_addjob(payload: dict, account_number: str | None) -> dict:
    creds = account_credentials(account_number)
    token = base64.b64encode(f"{creds['username']}:{creds['password']}".encode()).decode()
    url = f"{creds['base_url'].rstrip('/')}/AddJob"

    logger.info(
        "Indigo AddJob request (account %s) → %s\n%s",
        account_number, url, json.dumps(payload, indent=2),
    )

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            response = await client.post(
                url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "X-Indigo-Access-Token": token,
                },
            )
        except httpx.HTTPError as exc:
            logger.error("Indigo AddJob request failed before a response arrived: %s", exc)
            raise IndigoRequestError(f"Could not reach Indigo: {exc}") from exc

    logger.info("Indigo AddJob response ← %s\n%s", response.status_code, response.text[:2000])

    if response.status_code >= 400:
        raise IndigoRequestError(f"Indigo AddJob failed: {response.status_code} {response.text[:500]}")

    return response.json()
