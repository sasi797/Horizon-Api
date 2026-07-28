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


def parse_dimensions(value: str | None) -> tuple[float, float, float]:
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", value or "")]
    nums += [0.0, 0.0, 0.0]
    return nums[0], nums[1], nums[2]


def to_indigo_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    # collection_at/delivery_at are wall-clock times lifted straight from the
    # HAWB PDF and stored with a fake UTC tag purely to fit a timestamptz
    # column — not real UTC instants. strftime formats the stored digits
    # as-is (no timezone conversion), matching the frontend's approach of
    # reading the ISO string's digits directly.
    return value.strftime("%Y-%m-%dT%H:%M:%S.000")


def group_jobs_by_route(jobs: list[HawbJob]) -> list[list[HawbJob]]:
    groups: dict[tuple[str | None, str | None], list[HawbJob]] = {}
    order: list[tuple[str | None, str | None]] = []
    for job in jobs:
        key = (job.shipper, job.consignee)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(job)
    return [groups[key] for key in order]


def build_indigo_addjob_payload(
    service_type: str,
    manifest: HawbManifest,
    job_groups: list[list[HawbJob]],
) -> dict:
    jobs_payload = []
    for group in job_groups:
        job = group[0]
        length, width, height = parse_dimensions(job.dimensions)
        col = city_and_postcode_line(job.shipper)
        dele = city_and_postcode_line(job.consignee)
        total_packs = sum(j.package_qty or 0 for j in group)
        total_weight = sum(float(j.weight_kg or 0) for j in group)
        special_insts = " — ".join(dict.fromkeys(j.special_handling for j in group if j.special_handling))
        col_split = split_address(job.shipper)
        del_split = split_address(job.consignee)

        jobs_payload.append({
            "JobGuid": uuid.uuid4().hex,
            "CustomerNumber": manifest.account_number or "",
            "ServiceType": service_type,
            "VehicleType": manifest.vehicle_size or "",
            "JobReference": job.hawb_number,
            "JobReference2": "",
            "BookedBy": "",
            "RequestedBy": "Horizon Web",
            "ColDateTime": to_indigo_datetime(job.collection_at),
            "ColCompany": col_split["name"],
            "ColContact": job.shipper_contact or "",
            "ColAddress1": col_split["address"],
            "ColAddress2": "",
            "ColAddress3": "",
            "ColTown": col["town"],
            "ColPostcode": col["postcode"],
            "ColCountry": address_country(job.shipper),
            "ColTelephone": job.shipper_phone or "",
            "ColEmail": "",
            "ColInsts": "",
            "ColReadyAt": "",
            "ColPremisesClose": "",
            "DelDateTime": to_indigo_datetime(job.delivery_at),
            "DelCompany": del_split["name"],
            "DelContact": job.consignee_contact or "",
            "DelAddress1": del_split["address"],
            "DelAddress2": "",
            "DelAddress3": "",
            "DelTown": dele["town"],
            "DelPostcode": dele["postcode"],
            "DelCountry": address_country(job.consignee),
            "DelTelephone": job.consignee_phone or "",
            "DelInsts": "",
            "DelReadyAt": "",
            "DelPremisesClose": "",
            "Packs": total_packs,
            "Weight": total_weight,
            "SpecialInsts": special_insts,
            "Length": length,
            "Width": width,
            "Height": height,
            "Fragile": 0,
            "Security": 0,
            "ConsignmentNo": job.hawb_number,
            "Insurance": 0,
            "InsuranceValue": 0,
        })

    return {"Jobs": {"Job": jobs_payload}}


class IndigoRequestError(Exception):
    pass


async def call_indigo_addjob(payload: dict) -> dict:
    token = base64.b64encode(f"{settings.INDIGO_USERNAME}:{settings.INDIGO_PASSWORD}".encode()).decode()
    url = f"{settings.INDIGO_BASE_URL.rstrip('/')}/AddJob"

    logger.info("Indigo AddJob request → %s\n%s", url, json.dumps(payload, indent=2))

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
