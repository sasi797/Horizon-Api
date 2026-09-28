"""Server-side mytransport.co.uk `order_import` integration — replaces the
retired Indigo (NPA) `AddJob` integration.

Ported from Horizon-Web's `docs/mytransport-export-integration.md`, which has
the full field-by-field reference for this payload.

Runs server-side (not from the browser) for the same reasons the Indigo call
did: the login must never ship in the frontend bundle, and a PHP endpoint
like this one is unlikely to have CORS enabled for a browser origin.
"""

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


# The postcode is the only reliable anchor for where the address ends —
# without a match, there's no way to tell a real country line from just
# another address line (e.g. a building/site name), so we decline rather
# than guess. Searches from the last line backward, same as
# city_and_postcode_line below, so both agree on which line the postcode is
# on. Mirrors Horizon-Web's `parseAddressParts` in `hawbFormat.ts`.
#
# EasyTrans rejects an explicit empty country ("Unknown or disabled country:
# ", errorno 33) — it only falls back to the carrier's own country when the
# field is omitted entirely, not when it's sent blank. Domestic UK addresses
# on these HAWBs routinely have no country line at all (the blob just ends on
# its "Town, Postcode" line), so a UK-format postcode with no country line
# after it defaults to "United Kingdom" — the exact string this carrier's
# EasyTrans environment expects (confirmed from its own address-country
# dropdown). Mirrors Horizon-Web's `isUkAddress` fallback in `hawbFormat.ts`;
# none of the other countries these manifests reach use that postcode format.
#
# An *explicit* country line can be just as much of a problem: confirmed live
# (errorno 33, "Unknown or disabled country: UK") that EasyTrans' country
# field only recognizes the exact string "United Kingdom" — a HAWB whose own
# country line reads "UK" (or another constituent-nation alias) gets rejected
# just the same as a blank one, so any UK alias is normalized to that exact
# string here too; a genuine other country is passed through as extracted.
#
# Some source HAWBs extract the country twice, as two separate trailing lines
# in different casing (e.g. "UNITED KINGDOM" then "United Kingdom") — a join
# of both would send the literal string "UNITED KINGDOM, United Kingdom",
# which EasyTrans also rejects (confirmed live, same errorno 33). When every
# line in the tail is itself a UK alias, they're the same fact repeated, not
# a two-part country name — collapse them to one "United Kingdom" instead of
# joining raw.
def address_country(value: str | None) -> str:
    lines = _lines(value)
    for i in range(len(lines) - 1, 0, -1):
        line = lines[i]
        match = UK_POSTCODE_RE.search(line) or EIRCODE_RE.search(line) or NUMERIC_POSTCODE_RE.search(line)
        if match:
            tail = lines[i + 1 :]
            if tail and all(_normalize_country_token(t) == "uk" for t in tail):
                return "United Kingdom"
            explicit = ", ".join(tail)
            if explicit:
                return "United Kingdom" if _normalize_country_token(explicit) == "uk" else explicit
            if UK_POSTCODE_RE.search(line):
                return "United Kingdom"
            return ""
    return ""


# The "Town, Postcode" line reliably sits second-to-last, right before the
# country line — search from the end backward, skipping the first line
# (company/name), so a street number earlier in the address never gets
# mistaken for the postcode.
#
# That assumes town and postcode share a line, which isn't always true: a real
# sample ends "Harrogate, North Yorkshire,\nHG3 1PY, United Kingdom" — the
# postcode shares its line with the country instead, and the town sits alone
# on the line above. When there's nothing before the postcode match on its own
# line, fall back to the first comma-segment of the previous line rather than
# silently returning an empty town (guarded so it never reads the name line).
def city_and_postcode_line(value: str | None) -> dict:
    lines = _lines(value)
    for i in range(len(lines) - 1, 0, -1):
        line = lines[i]
        match = UK_POSTCODE_RE.search(line) or EIRCODE_RE.search(line) or NUMERIC_POSTCODE_RE.search(line)
        if match:
            postcode = match.group(0).upper()
            before = line[: match.start()].strip().rstrip(",").strip()
            if before:
                town = before.split(",")[0].strip()
            elif i > 1:
                town = lines[i - 1].split(",")[0].strip()
            else:
                town = ""
            return {"town": town, "postcode": postcode}
    return {"town": "", "postcode": ""}


def to_mytransport_date_time(value: datetime | None) -> tuple[str, str]:
    if value is None:
        return "", ""
    # collection_at/delivery_at are wall-clock times lifted straight from the
    # HAWB PDF and stored with a fake UTC tag purely to fit a timestamptz
    # column — not real UTC instants, so this reads the stored digits as-is.
    return value.strftime("%Y-%m-%d"), value.strftime("%H:%M")


# order_packages length/width/height have no dedicated columns on HawbJob —
# `dimensions` is the free-text "L W H" string extraction reads straight off
# the HAWB's Dims (cms) column (e.g. "40 40 40"). Pulling the first three
# numbers out of it is a best-effort reading of whatever format the source
# document used; missing/unparseable defaults to 0.0, same as EasyTrans' own
# default for an omitted value.
def _parse_dimensions(value: str | None) -> tuple[float, float, float]:
    numbers = [float(n) for n in re.findall(r"\d+\.?\d*", value or "")[:3]]
    numbers += [0.0] * (3 - len(numbers))
    return numbers[0], numbers[1], numbers[2]


def _normalize_identity(value: str) -> str:
    no_apostrophes = value.replace("'", "").replace("’", "")
    return re.sub(r"[.,]+$", "", re.sub(r"\s+", " ", no_apostrophes.lower())).strip()


# Extractors write the country line in whichever form the source document
# used, and constituent nations turn up as often as the union's own name.
# Mirrors Horizon-Web's UK_COUNTRY_NAMES in hawbFormat.ts.
UK_COUNTRY_NAMES = {
    "uk", "gb", "united kingdom", "great britain", "england", "scotland", "wales",
    "northern ireland", "united kingdom of great britain and northern ireland",
}


def _normalize_country_token(value: str) -> str:
    """'UK' vs 'United Kingdom' vs 'GB' are the same fact written three ways —
    collapse any of them to one token so an identity match isn't defeated by
    which alias a given source document happened to use."""
    cleaned = re.sub(r"\s+", " ", re.sub(r"[^a-z ]", " ", value.lower())).strip()
    return "uk" if cleaned in UK_COUNTRY_NAMES else cleaned


# Words that tell you a company's legal form or that it operates in the UK,
# not which company it is — stripped off the end of a name (possibly several
# at once, e.g. "... NHS Foundation Trust") so that they don't defeat a
# same-company match just because one HAWB's extraction included the suffix
# and another's didn't. Deliberately excludes anything that carries real
# identifying meaning ("Hospital", "Laboratory", "Services", "Institute" —
# two different real places can differ only by one of those). Mirrors
# Horizon-Web's TRAILING_NAME_BOILERPLATE in hawbFormat.ts.
TRAILING_NAME_BOILERPLATE = UK_COUNTRY_NAMES | {
    "nhs foundation trust", "nhs trust", "foundation trust", "trust", "foundation", "nhs",
    "ltd", "limited", "plc", "llc", "inc", "corp", "corporation", "co", "company",
}
TRAILING_NAME_BOILERPLATE_BY_LENGTH = sorted(TRAILING_NAME_BOILERPLATE, key=len, reverse=True)


def _strip_trailing_boilerplate(name: str) -> str:
    """Repeatedly strips a trailing boilerplate word/phrase off the end of an
    already-lowercased/whitespace-collapsed name — "guys and st thomas nhs
    foundation trust" needs three rounds ("trust", then "foundation", then
    "nhs") to reach the same "guys and st thomas" another HAWB for the same
    building might extract straight to."""
    current = name
    for _ in range(6):
        match = next(
            (token for token in TRAILING_NAME_BOILERPLATE_BY_LENGTH
             if current != token and current.endswith(f" {token}")),
            None,
        )
        if match is None:
            break
        current = current[: -(len(match) + 1)].strip()
    return current


def address_identity_key(value: str | None) -> str | None:
    """Same-location identity, mirroring Horizon-Web's `addressIdentityKey` in
    `src/lib/hawbFormat.ts`. Deliberately coarser than an exact string match:
    keyed on the company name and the site signal only, because the middle
    lines (floor, suite, punctuation) drift between HAWBs OCR'd from different
    source documents even when it's plainly the same building — with
    punctuation, trailing legal-entity boilerplate ("... NHS Foundation
    Trust" vs "... NHS Foundation" vs plain "..."), and country-alias quirks
    all ironed out first, since none of that is a different place, just a
    different way of writing the same one.

    The site signal is the tail after the final comma on the last line, not
    the whole line — some source documents extract with every line after the
    name comma-joined onto one ("Road, Centre, 10th Floor, North Wing, St
    Thomas' Hospital") instead of split across lines like a well-formed
    address ("Road" / "Centre" / "10th Floor, North Wing" / "St Thomas'
    Hospital"). In both shapes the recognizable site name is whatever sits
    after the final comma, so comparing on that instead of the raw last line
    matches the two shapes the same way."""
    lines = _lines(value)
    if not lines:
        return None
    name = _strip_trailing_boilerplate(_normalize_identity(lines[0]))
    if not name or name == "—":
        return None
    last = _normalize_country_token(lines[-1].split(",")[-1])
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


def group_jobs_by_merge(jobs: list[HawbJob]) -> list[tuple[str, list[HawbJob]]]:
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
    rejects those before they reach mytransport.

    A job with manual_group_id set (merged or force-standalone by hand on the
    manifest page) is fully excluded from the to:/contact: heuristic below —
    it neither contributes to a bucket nor can be pulled into one — and is
    grouped purely on manual_group_id instead. That keeps a manual override
    from dragging unrelated jobs into its bucket, and keeps unmerging one job
    from breaking the auto-grouping of the group's other members.

    Each group ships tagged with its merge kind ("to", "contact", "manual",
    or "single") so `validate_merge_groups` can tell a contact-tier merge
    (address mismatch expected to be rare and forgivable — see there) from a
    to-tier one (address mismatch is a real routing danger, always rejected)."""
    auto_jobs = [job for job in jobs if not job.manual_group_id]

    to_buckets: dict[str, list[HawbJob]] = {}
    for job in auto_jobs:
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
    for job in auto_jobs:
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
        key = f"manual:{job.manual_group_id}" if job.manual_group_id else (key_for_job.get(job.id) or f"single:{job.id}")
        groups.setdefault(key, []).append(job)
    return [(key.split(":", 1)[0], group) for key, group in groups.items()]


def validate_merge_groups(job_groups: list[tuple[str, list[HawbJob]]]) -> list[str]:
    """Every HAWB merged into one drop has to send the driver to one place, so a
    group is only bookable when its members agree on the Del/Coll leg — and,
    unless it's a contact-tier merge, on the address that leg stops at too.

    Address agreement isn't guaranteed by the merge rule itself: the to-tier
    keys on the "To", so a group whose members are collections is keyed on an
    address nobody visits, and its pickups can sit in different countries.
    Left unchecked the drop silently takes whichever HAWB sorted first and the
    rest vanish from the payload — a driver sent to California for a run out
    of St. Mary's. That's always rejected.

    A contact-tier merge is different: it's keyed on a named shipper contact
    on the assumption that the same person implies the same building, which
    mostly holds — the rare exception is a contact who legitimately serves a
    couple of sub-locations (e.g. a hospital's main site vs. its reception
    desk). Rejecting the whole export over that rare case is worse than the
    fix, so those groups are allowed through and `build_mytransport_order_payload`
    books the first member's address for all of them. Returns one
    plain-language problem per unbookable group."""
    problems: list[str] = []
    for kind, group in job_groups:
        if len(group) < 2:
            continue
        hawbs = ", ".join(j.hawb_number for j in group)
        if len({j.job_service_type for j in group}) > 1:
            problems.append(
                f"{hawbs} are merged into one stop but have different Del/Coll "
                "services — a merged stop is a single visit, so they must match."
            )
            continue
        if kind == "contact":
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


def resolve_end_point(manifest: HawbManifest) -> str | None:
    """The route's actual closing address — an explicit End point wins;
    otherwise, unless the dispatcher checked "Don't add End point as a
    destination" (skip_end_destination), the run closes the loop back at
    Start point, same as Horizon-Web's Run order preview defaults it
    (`effectiveEndPoint` / `endMatchesStart`, default on) before an End point
    is ever picked. Booking has to agree with what that preview already
    showed the dispatcher, or the stop count silently drops between the
    screen and the carrier."""
    if manifest.end_point:
        return manifest.end_point
    if manifest.skip_end_destination:
        return None
    return manifest.start_point


def is_backhaul_collection(job: HawbJob, manifest: HawbManifest) -> bool:
    """A collection whose pickup site is the same place as the manifest's End
    point isn't a real extra stop — the vehicle is already headed there as the
    run's last stop, so booking it again as its own destination would double
    up a visit mytransport doesn't need to be told about separately."""
    if job.job_service_type != "collection":
        return False
    identity = address_identity_key(job.shipper)
    return identity is not None and identity == address_identity_key(resolve_end_point(manifest))


def _matching_contact(address: str | None, jobs: list[HawbJob]) -> tuple[str, str]:
    """The Start point is picked from one of this manifest's own HAWB addresses
    (the exact same text as that job's shipper/consignee) — so the
    contact/phone for the origin destination can be borrowed from whichever
    HAWB that address actually came from, rather than left blank. Ported from
    Indigo's equivalent (`_matching_contact` in the retired indigo_export.py)."""
    if not address:
        return "", ""
    for job in jobs:
        if job.shipper == address:
            return job.shipper_contact or "", job.shipper_phone or ""
        if job.consignee == address:
            return job.consignee_contact or "", job.consignee_phone or ""
    return "", ""


def _destination(
    address: str | None,
    collect_deliver: int,
    contact: str,
    phone: str,
    remark: str,
    waybillno: str = "",
    customer_reference: str = "",
    delivery_date: str = "",
    delivery_time: str = "",
) -> dict:
    split = split_address(address)
    addr_line = city_and_postcode_line(address)
    return {
        "collect_deliver": collect_deliver,
        "company_name": split["name"],
        "contact": contact,
        "address": split["address"],
        "houseno": "",
        "postal_code": addr_line["postcode"],
        "city": addr_line["town"],
        "country": address_country(address),
        "telephone": phone,
        "destination_remark": remark,
        "customer_reference": customer_reference,
        "waybillno": waybillno,
        "delivery_date": delivery_date,
        "delivery_time": delivery_time,
    }


def _collapse_same_stop_groups(
    job_groups: list[list[HawbJob]],
    manifest: HawbManifest,
) -> list[list[HawbJob]]:
    """Two different merge groups (no shared consignee or shipper contact, so
    `group_jobs_by_merge` kept them apart) can still resolve to the exact same
    physical stop — e.g. two collections from the same building, anywhere in
    the run order, whose company name was OCR'd slightly differently per
    HAWB. Booking them as separate destinations would send the driver to the
    same building twice, so any earlier group (not just the immediately
    preceding one) whose Del/Coll leg and resolved stop address agree absorbs
    this one — the existing per-group combining logic in the caller
    (waybill/contact/remark/package) then handles the folded group exactly
    like a real merge. This is also what makes editing a job's address in the
    Destinations preview to fix an extraction mismatch immediately fold it
    into its real match on the next export. An all-backhaul group is left as
    its own entry (the caller drops it regardless) and never absorbs or is
    absorbed by another."""
    collapsed: list[list[HawbJob]] = []
    for group in job_groups:
        if all(is_backhaul_collection(j, manifest) for j in group):
            collapsed.append(group)
            continue
        job = group[0]
        identity = (
            address_identity_key(stop_address(job))
            if job.job_service_type in ("collection", "delivery")
            else None
        )
        merged = False
        if identity is not None:
            for i, existing in enumerate(collapsed):
                if all(is_backhaul_collection(j, manifest) for j in existing):
                    continue
                existing_job = existing[0]
                if (
                    existing_job.job_service_type == job.job_service_type
                    and address_identity_key(stop_address(existing_job)) == identity
                ):
                    collapsed[i] = existing + group
                    merged = True
                    break
        if not merged:
            collapsed.append(group)
    return collapsed


def build_mytransport_order_payload(
    manifest: HawbManifest,
    job_groups: list[list[HawbJob]],
) -> dict:
    """One manifest books as exactly one mytransport order: `order_destinations[0]`
    is the manifest's own Start point (`collect_deliver: 0`, the pickup), and
    every HAWB stop in between — one per group from `group_jobs_by_merge`,
    after `_collapse_same_stop_groups` folds together any adjacent groups that
    resolve to the same physical address — rides along as its own destination
    (`collect_deliver` 0 for a Collection leg, 1 for a Delivery leg), with an
    `order_packages` entry pointing back at it by its 1-based position in
    `order_destinations`."""
    job_groups = _collapse_same_stop_groups(job_groups, manifest)
    all_jobs = [job for group in job_groups for job in group]
    stop_times = [
        (j.collection_at if j.job_service_type == "collection" else j.delivery_at)
        for j in all_jobs
    ]
    date_str, time_str = to_mytransport_date_time(min((t for t in stop_times if t is not None), default=None))

    origin_contact, origin_phone = _matching_contact(manifest.start_point, all_jobs)
    destinations = [_destination(manifest.start_point, 0, origin_contact, origin_phone, "")]
    packages = []
    for group in job_groups:
        # A merged stop is one physical visit — if every HAWB in it is a
        # backhaul collection at the End point, the whole stop is skipped from
        # export (the vehicle is already headed there as the run's last leg).
        if all(is_backhaul_collection(j, manifest) for j in group):
            continue
        job = group[0]
        is_collection = job.job_service_type == "collection"
        # Reading the leg and the address off the first member is shorthand
        # for a to-tier or manual group, where validate_merge_groups has
        # already rejected any group whose members differ on either. For a
        # contact-tier group it's a real choice: members may genuinely
        # disagree on address, and the first one booked wins for the group.
        address = job.shipper if is_collection else job.consignee
        contact = (job.shipper_contact if is_collection else job.consignee_contact) or ""
        phone = (job.shipper_phone if is_collection else job.consignee_phone) or ""
        remark = " — ".join(dict.fromkeys(j.special_handling for j in group if j.special_handling))
        waybillno = ", ".join(j.hawb_number for j in group)
        reference = ", ".join(dict.fromkeys(
            ref for j in group
            if (ref := (j.shipper_reference if is_collection else j.consignee_reference))
        ))
        group_times = [(j.collection_at if is_collection else j.delivery_at) for j in group]
        group_date, group_time = to_mytransport_date_time(
            min((t for t in group_times if t is not None), default=None)
        )
        destinations.append(_destination(
            address, 0 if is_collection else 1, contact, phone, remark,
            waybillno=waybillno, customer_reference=reference,
            delivery_date=group_date, delivery_time=group_time,
        ))
        # Confirmed live against mytransport: order_packages can only point at
        # a deliver destination (collect_deliver: 1) — pointing one at a
        # collect_deliver: 0 stop is rejected outright ("destinationno N is
        # not a deliver destination", errorno 43). A collection-leg stop still
        # gets its destination entry above so the driver's route includes it;
        # it just carries no package line.
        if not is_collection:
            length, width, height = _parse_dimensions(job.dimensions)
            # The goods description belongs here, not the HAWB number — that
            # moved to the destination's own waybillno above now that the
            # field exists. Falls back to the HAWB numbers only if extraction
            # never captured a package content description.
            contents = ", ".join(dict.fromkeys(
                p.get("content_description") for p in (job.packages or []) if p.get("content_description")
            ))
            packages.append({
                "deliver_destinationno": len(destinations),
                "amount": sum(j.package_qty or 0 for j in group),
                "weight": sum(float(j.weight_kg or 0) for j in group),
                "length": length,
                "width": width,
                "height": height,
                "description": contents or waybillno,
            })

    # The route's end point is always the final destination — regardless of
    # whether a job already supplied a real Delivery leg. An explicit End
    # point wins; otherwise (see `resolve_end_point`) it defaults to Start
    # point, same as the Run order preview already showed the dispatcher
    # before export — often the same address as Start point (a local
    # collection round that hands off back at its own hub), but not
    # necessarily — a manifest can just as well close at a real third-party
    # site like a hospital. Confirmed against a real EasyTrans order that
    # even a same-building "return to depot" leg is booked as its own
    # Delivery destination, not left implicit. `skip_end_destination` is the
    # escape hatch for the rare manifest that genuinely has nowhere to close
    # the loop.
    end_point = resolve_end_point(manifest)
    if end_point:
        end_contact, end_phone = _matching_contact(end_point, all_jobs)
        destinations.append(_destination(end_point, 1, end_contact, end_phone, ""))

    order = {
        "date": date_str,
        "time": time_str,
        "status": "submit",
        "productno": settings.MYTRANSPORT_PRODUCTNO,
        "customerno": settings.MYTRANSPORT_CUSTOMERNO,
        "remark": manifest.job_reference or "",
        "order_destinations": destinations,
        "order_packages": packages,
    }

    return {
        "authentication": {
            "username": settings.MYTRANSPORT_USERNAME,
            "password": settings.MYTRANSPORT_PASSWORD,
            "type": "order_import",
            "mode": "effect",
            "version": 2,
        },
        "orders": [order],
    }


class MytransportRequestError(Exception):
    pass


def is_success_response(data: dict) -> bool:
    """Per EasyTrans' JSON order import docs (the software mytransport.co.uk
    runs): a rejection is always `{"error": {"errorno": ..., "error_description":
    ...}}` with HTTP 200, and a success is always `{"result": {"mode": ...,
    "new_ordernos": [...], ...}}` — there is no other response shape. Falls
    back to trusting the HTTP status (already checked < 400 by the caller) only
    if the body is neither, which shouldn't happen against a real EasyTrans
    endpoint."""
    if not isinstance(data, dict):
        return True
    if "error" in data:
        return False
    if "result" in data:
        return True
    return True


async def call_mytransport_import(payload: dict) -> dict:
    url = settings.MYTRANSPORT_BASE_URL

    logger.info("mytransport order_import request → %s\n%s", url, json.dumps(payload, indent=2))

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            response = await client.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            logger.error("mytransport order_import request failed before a response arrived: %s", exc)
            raise MytransportRequestError(f"Could not reach mytransport: {exc}") from exc

    logger.info("mytransport order_import response ← %s\n%s", response.status_code, response.text[:2000])

    if response.status_code >= 400:
        raise MytransportRequestError(f"mytransport order_import failed: {response.status_code} {response.text[:500]}")

    try:
        return response.json()
    except ValueError:
        # This is a PHP endpoint — a plain-text/HTML success body instead of
        # JSON wouldn't be surprising.
        return {"raw": response.text}
