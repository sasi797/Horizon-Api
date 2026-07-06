import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.services import hawb_extract
from app.storage import upload_bytes

logger = logging.getLogger(__name__)


async def process_email_attachments(
    *, message_id: str, sender_email: str, subject: str, pdf_attachments: list[dict]
) -> None:
    """Entry point called from the email poll loop for one inbound message."""
    from app.database import AsyncSessionLocal
    from app.models.hawb import HawbProcessedEmail

    async with AsyncSessionLocal() as db:
        exists = await db.scalar(
            select(HawbProcessedEmail.message_id).where(HawbProcessedEmail.message_id == message_id).limit(1)
        )
        if exists:
            return

    for att in pdf_attachments:
        try:
            raw = att.get("data") or b""
            filename = att.get("filename") or "document.pdf"
            await ingest_pdf(
                raw, filename,
                source_message_id=message_id, sender_email=sender_email, subject=subject,
            )
        except Exception:
            logger.error("HAWB ingest failed for attachment in message %s", message_id, exc_info=True)

    async with AsyncSessionLocal() as db:
        await db.execute(
            pg_insert(HawbProcessedEmail).values(message_id=message_id).on_conflict_do_nothing()
        )
        await db.commit()


async def ingest_pdf(file_bytes: bytes, filename: str, *, source_message_id: str | None, sender_email: str | None, subject: str | None):
    """Upload a HAWB PDF, extract jobs from it, and persist a HawbDocument + HawbJob rows."""
    from app.database import AsyncSessionLocal
    from app.models.hawb import HawbDocument, HawbJob

    key = f"{settings.s3_prefix}/hawb/{source_message_id or 'manual'}/{filename}"
    await upload_bytes(file_bytes, key, content_type="application/pdf")

    error_message: str | None = None
    try:
        jobs_data = await asyncio.get_event_loop().run_in_executor(
            None, hawb_extract.extract_jobs, file_bytes, filename
        )
    except Exception as e:
        logger.error("HAWB extraction failed for %s", filename, exc_info=True)
        jobs_data = []
        error_message = f"Extraction failed: {e}"

    async with AsyncSessionLocal() as db:
        document = HawbDocument(
            source_message_id=source_message_id,
            sender_email=sender_email,
            subject=subject,
            filename=filename,
            storage_bucket=settings.s3_bucket or "horizon-dev",
            storage_key=key,
            processed_at=datetime.now(timezone.utc),
            job_count=0,
            status="failed" if error_message else "processed",
            error_message=error_message,
        )
        db.add(document)
        await db.flush()

        inserted = 0
        skip_notes: list[str] = []
        for job in jobs_data:
            hawb_number = (job.get("hawb_number") or "").strip()
            if not hawb_number:
                continue
            existing = await db.scalar(select(HawbJob.id).where(HawbJob.hawb_number == hawb_number))
            if existing:
                skip_notes.append(f"Duplicate HAWB skipped: {hawb_number}")
                continue
            db.add(HawbJob(
                document_id=document.id,
                hawb_number=hawb_number,
                page_start=job.get("page_start"),
                shipper=job.get("shipper"),
                consignee=job.get("consignee"),
                collection_at=_parse_dt(job.get("collection_at")),
                delivery_at=_parse_dt(job.get("delivery_at")),
                package_qty=job.get("package_qty"),
                weight_kg=job.get("weight_kg"),
                dangerous_goods=bool(job.get("dangerous_goods", False)),
                dangerous_goods_notes=job.get("dangerous_goods_notes"),
                client_account=job.get("client_account"),
                package_sequence=job.get("package_sequence"),
                shipper_contact=job.get("shipper_contact"),
                shipper_phone=job.get("shipper_phone"),
                shipper_reference=job.get("shipper_reference"),
                consignee_contact=job.get("consignee_contact"),
                consignee_phone=job.get("consignee_phone"),
                consignee_reference=job.get("consignee_reference"),
                temperature_range=job.get("temperature_range"),
                dimensions=job.get("dimensions"),
                volumetric_weight_kg=job.get("volumetric_weight_kg"),
                declared_value=job.get("declared_value"),
                declared_value_currency=job.get("declared_value_currency"),
                direction=job.get("direction"),
                special_handling=job.get("special_handling"),
                packages=job.get("packages") or [],
                extracted_data=job,
            ))
            inserted += 1

        document.job_count = inserted
        if skip_notes:
            note = "; ".join(skip_notes)
            document.error_message = note if not document.error_message else f"{document.error_message}; {note}"

        await db.commit()
        await db.refresh(document)
        return document


def _parse_dt(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None
