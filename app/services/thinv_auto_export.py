"""Auto-export of Thinventory (THINV-nnnnnnn) manifests to mytransport/EasyTrans.

A Thinventory delivery note always books the same way — no Start/End point
(each job is just its own Collection and Delivery stop), same customer, same
service/vehicle — so as soon as ingest has built the manifest
this fills those defaults and books it into EasyTrans with no manual click.
If anything blocks the booking, the manifest keeps its defaults, records
the error against EasyTrans (so the manifest page shows it and offers Retry
export) and an alert email goes to the shared mailbox.
"""

import logging

from sqlalchemy import select

from app.core.config import settings
from app.models.hawb import HawbJob, HawbManifest
from app.services import mytransport_export

logger = logging.getLogger(__name__)

THINV_REFERENCE_PREFIX = "THINV-"

# Dropdown values (module 'manifest') exactly as stored — the option `value`,
# not its display label.
DEFAULT_CUSTOMER_NUMBER = "2050"  # Thinventory Field Support Limited - 2050
DEFAULT_SERVICE_TYPE = "Sameday"
DEFAULT_VEHICLE_SIZE = "Small Van"

# Skipped by the inbox poller so the alert, sent to the same mailbox it
# polls, is never mistaken for an inbound booking email.
ALERT_SUBJECT_PREFIX = "[Horizon] THINV auto-export failed"
EXTRACTION_ALERT_SUBJECT_PREFIX = "[Horizon] THINV extraction failed"


def is_thinventory_manifest(manifest: HawbManifest) -> bool:
    return (manifest.reference_number or "").startswith(THINV_REFERENCE_PREFIX)


def _is_uk(address: str | None) -> bool:
    return mytransport_export.address_country(address) == "United Kingdom"


def _default_job_service_type(job: HawbJob) -> str | None:
    """A Thinventory run is Start point -> collect -> deliver -> End point, so a
    UK-to-UK note needs both legs: collection_and_delivery (two stops). One end
    in the UK follows that end, as Horizon-Web's defaultServiceType does. Neither
    end in the UK stays unset for a person to pick, which fails the export and
    sends the alert email."""
    from_uk, to_uk = _is_uk(job.shipper), _is_uk(job.consignee)
    if from_uk and to_uk:
        return "collection_and_delivery"
    if from_uk:
        return "collection"
    if to_uk:
        return "delivery"
    return None


def apply_thinventory_defaults(manifest: HawbManifest, jobs: list[HawbJob]) -> None:
    """Fill only what's blank — never overwrites a value a person has set. No
    Start/End point: a Thinventory job books as just its own Collection and
    Delivery stops."""
    manifest.customer_number = manifest.customer_number or DEFAULT_CUSTOMER_NUMBER
    manifest.service_type = manifest.service_type or DEFAULT_SERVICE_TYPE
    manifest.vehicle_size = manifest.vehicle_size or DEFAULT_VEHICLE_SIZE
    manifest.job_reference = manifest.job_reference or manifest.reference_number
    for job in jobs:
        if not job.job_service_type:
            job.job_service_type = _default_job_service_type(job)


async def _book_mytransport(manifest: HawbManifest, jobs: list[HawbJob]) -> None:
    """Validates like the manual Export, then books EasyTrans only. Raises
    ValueError with the reason on anything that blocks or fails the booking."""
    from app.routers.hawb import _run_mytransport_export

    if not (manifest.customer_number or "").strip().isdigit():
        raise ValueError("Missing required field before export: Customer number")
    if not jobs:
        raise ValueError("Manifest has no jobs to export")

    mytransport_export.normalize_uk_round_trips(jobs)
    missing_service = [
        j.hawb_number for j in jobs
        if j.job_service_type not in ("collection", "delivery", "collection_and_delivery")
    ]
    if missing_service:
        raise ValueError("Del/Coll must be set on every HAWB before export. Missing on: " + ", ".join(missing_service))

    job_groups = mytransport_export.group_jobs_by_merge(list(jobs))
    conflicts = mytransport_export.validate_merge_groups(job_groups)
    if conflicts:
        raise ValueError(" ".join(conflicts))
    payload = mytransport_export.build_mytransport_order_payload(manifest, [group for _, group in job_groups])

    if len(payload["orders"][0]["order_destinations"]) < 2:
        raise ValueError("EasyTrans needs at least two destinations (a Collection and a Delivery) and this manifest has fewer.")

    result = await _run_mytransport_export(manifest, payload, already_booked=False)
    manifest.mytransport_export_status = result.status
    manifest.mytransport_export_error = result.error
    if result.status != "booked":
        raise ValueError(result.error or "EasyTrans did not accept the booking")
    manifest.mytransport_order_no = result.reference
    manifest.mytransport_tracking_url = result.tracking_url


async def auto_export_thinventory_manifest(manifest_id) -> None:
    from datetime import datetime, timezone
    from app.database import AsyncSessionLocal

    reference = str(manifest_id)
    error: str | None = None
    async with AsyncSessionLocal() as db:
        manifest = await db.get(HawbManifest, manifest_id)
        if (
            not manifest or not is_thinventory_manifest(manifest) or manifest.status != "open"
            or manifest.mytransport_export_status == "booked"
        ):
            return
        reference = manifest.reference_number
        jobs = (await db.execute(
            select(HawbJob).where(HawbJob.manifest_id == manifest_id).order_by(HawbJob.manifest_sequence)
        )).scalars().all()

        # Defaults are saved even when the booking fails, so the manifest page
        # opens already filled in and only the real problem is left to fix.
        apply_thinventory_defaults(manifest, jobs)
        try:
            await _book_mytransport(manifest, jobs)
        except ValueError as exc:
            error = str(exc)
            manifest.mytransport_export_status = "failed"
            manifest.mytransport_export_error = error
        except Exception as exc:
            logger.error("THINV auto-export crashed for %s", reference, exc_info=True)
            error = f"Unexpected error: {exc}"
            manifest.mytransport_export_status = "failed"
            manifest.mytransport_export_error = error
        else:
            now = datetime.now(timezone.utc)
            for job in jobs:
                job.locked = True
                job.status = "manifested"
                job.manifested_at = now
            manifest.status = "exported"
            manifest.exported_at = now
        await db.commit()

    if error:
        await _send_failure_email(reference, str(manifest_id), error)


async def _send_failure_email(reference: str, manifest_id: str, error: str) -> None:
    link = f"{settings.FRONTEND_BASE_URL.rstrip('/')}/dashboard/manifests/{manifest_id}" if settings.FRONTEND_BASE_URL else ""
    body = (
        f"Manifest {reference} could not be exported to EasyTrans automatically.\n\n"
        f"Reason: {error}\n\n"
        "Open the manifest, correct the details, then click Export manifest again."
        + (f"\n\n{link}" if link else "")
    )
    await _send_alert(f"{ALERT_SUBJECT_PREFIX} — {reference}", body, reference, error)


async def send_extraction_failure_email(reference: str, manifest_id: str, error: str) -> None:
    """Alert for a Thinventory PDF whose extraction failed, so the manifest
    never sits in Failed unnoticed. Never raises."""
    link = f"{settings.FRONTEND_BASE_URL.rstrip('/')}/dashboard/manifests/{manifest_id}" if settings.FRONTEND_BASE_URL else ""
    body = (
        f"Manifest {reference} could not be read from its Thinventory delivery note.\n\n"
        f"Reason: {error}\n\n"
        "Open the manifest and click Retry on the Extract column once the cause is fixed."
        + (f"\n\n{link}" if link else "")
    )
    await _send_alert(f"{EXTRACTION_ALERT_SUBJECT_PREFIX} — {reference}", body, reference, error)


async def _send_alert(subject: str, body: str, reference: str, error: str) -> None:
    from app.routers.email_messages import _send_via_graph

    # ALLOWED_SENDER is an external (Gmail) address Graph can't send as, so the
    # alert is sent from the company mailbox to the control address.
    sender = (settings.MAILBOX_EMAIL or "").strip()
    recipient = (settings.THINV_ALERT_EMAIL or "").strip() or sender
    if not sender:
        logger.error("THINV alert for %s not sent, MAILBOX_EMAIL isn't set: %s", reference, error)
        return
    try:
        await _send_via_graph(sender, [recipient], subject, body, [])
    except Exception:
        logger.error("THINV alert email for %s could not be sent", reference, exc_info=True)


async def auto_export_thinventory_manifests(manifest_ids) -> None:
    """Never raises — a problem here must not fail the ingest that called it."""
    for manifest_id in manifest_ids:
        try:
            await auto_export_thinventory_manifest(manifest_id)
        except Exception:
            logger.error("THINV auto-export failed for manifest %s", manifest_id, exc_info=True)
