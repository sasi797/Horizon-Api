import asyncio
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.services import hawb_extract
from app.storage import upload_bytes

logger = logging.getLogger(__name__)


def _is_blind_filename(filename: str) -> bool:
    return "mf-pcs" in filename.lower()


async def process_email_attachments(
    *, message_id: str, sender_email: str, subject: str, pdf_attachments: list[dict], body_text: str | None = None
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

    try:
        await ingest_email_batch(
            pdf_attachments,
            source_message_id=message_id, sender_email=sender_email, subject=subject, email_body=body_text,
        )
    except Exception:
        logger.error("HAWB batch ingest failed for message %s", message_id, exc_info=True)

    async with AsyncSessionLocal() as db:
        await db.execute(
            pg_insert(HawbProcessedEmail).values(message_id=message_id).on_conflict_do_nothing()
        )
        await db.commit()


async def ingest_email_batch(
    pdf_attachments: list[dict], *,
    source_message_id: str | None, sender_email: str | None, subject: str | None, email_body: str | None,
):
    """Extract every PDF attachment in one email, cross-reference plain-label
    jobs against MF-PCS booking-form jobs by hawb_number, merge matches, and
    persist HawbDocument/HawbJob/HawbManifest rows for the whole batch.

    A blind (MF-PCS-named) attachment carries a shipping label whose true
    collection/shipper identity may be withheld; a companion plain-HAWB
    attachment in the same email sharing the same hawb_number, plus the
    email's own body text, supply what's missing. Most HAWBs in a plain
    attachment have no MF-PCS counterpart and are persisted unchanged.

    "One PDF in, one manifest out" still holds for plain documents (and for
    MF-PCS documents whose jobs go unmatched); a merged blind job attaches to
    its plain document's manifest instead of getting one of its own.
    """
    from app.database import AsyncSessionLocal
    from app.models.hawb import HawbDocument, HawbJob, HawbJobPendingUpdate, HawbManifest

    blind_atts = [a for a in pdf_attachments if _is_blind_filename(a.get("filename") or "")]
    plain_atts = [a for a in pdf_attachments if not _is_blind_filename(a.get("filename") or "")]

    async with AsyncSessionLocal() as db:
        # --- Extract phase: one HawbDocument per attachment, jobs held in memory ---
        plain_entries: list[tuple[HawbDocument, list[dict]]] = []
        for att in plain_atts:
            doc, jobs_data = await _extract_and_create_document(
                db, att, source_kind="plain",
                source_message_id=source_message_id, sender_email=sender_email, subject=subject, email_body=email_body,
            )
            plain_entries.append((doc, jobs_data))

        blind_entries: list[tuple[HawbDocument, list[dict]]] = []
        for att in blind_atts:
            doc, jobs_data = await _extract_and_create_document(
                db, att, source_kind="blind",
                source_message_id=source_message_id, sender_email=sender_email, subject=subject, email_body=email_body,
            )
            blind_entries.append((doc, jobs_data))

        await db.flush()  # assign ids to every HawbDocument before we reference them

        # --- Match phase: hawb_number -> (plain HawbDocument, plain job dict) ---
        plain_by_hawb: dict[str, tuple[HawbDocument, dict]] = {}
        for doc, jobs_data in plain_entries:
            for job in jobs_data:
                hawb_number = (job.get("hawb_number") or "").strip()
                if hawb_number:
                    plain_by_hawb[hawb_number] = (doc, job)

        merged_hawb_numbers: set[str] = set()
        jobs_to_insert: list[HawbJob] = []

        # --- Merge phase: resolve each MF-PCS candidate against its plain companion ---
        for blind_doc, blind_jobs_data in blind_entries:
            for blind_job in blind_jobs_data:
                hawb_number = (blind_job.get("hawb_number") or "").strip()
                if not hawb_number:
                    continue
                match = plain_by_hawb.get(hawb_number)
                if match:
                    plain_doc, plain_job = match
                    try:
                        merged = await asyncio.get_event_loop().run_in_executor(
                            None, hawb_extract.merge_blind_job, plain_job, blind_job, email_body
                        )
                    except Exception:
                        logger.error("HAWB blind merge failed for %s", hawb_number, exc_info=True)
                        merged = {**plain_job, **{k: v for k, v in blind_job.items() if v}}
                    jobs_to_insert.append(_build_job_row(
                        merged, document_id=plain_doc.id, blind_document_id=blind_doc.id,
                        source_kind="blind", status="pending_review",
                    ))
                    merged_hawb_numbers.add(hawb_number)
                else:
                    # No companion plain HAWB in this email — persist from the booking form alone.
                    jobs_to_insert.append(_build_job_row(
                        blind_job, document_id=blind_doc.id, blind_document_id=None,
                        source_kind="blind", status="pending_review",
                    ))
                    note = f"No matching Plain HAWB found for merge: {hawb_number}"
                    blind_doc.error_message = note if not blind_doc.error_message else f"{blind_doc.error_message}; {note}"

        # --- Persist remaining (unmatched) plain jobs exactly as today ---
        for doc, jobs_data in plain_entries:
            for job in jobs_data:
                hawb_number = (job.get("hawb_number") or "").strip()
                if not hawb_number or hawb_number in merged_hawb_numbers:
                    continue
                jobs_to_insert.append(_build_job_row(
                    job, document_id=doc.id, blind_document_id=None,
                    source_kind="plain", status="ready_to_manifest",
                ))

        # --- Dedup against existing hawb_numbers: insert new, or queue a pending update ---
        # A hawb_number that already has a job is never silently dropped and never
        # auto-applied (even to an already-exported job) — it's queued for manual
        # review. If either side is blind-sourced, the "duplicate" usually isn't
        # one at all: it's the missing plain/MF-PCS companion for an existing job
        # that was persisted unmatched, now arriving in a later email — that case
        # gets a real merge (same as the in-email merge path) instead of a plain
        # field diff.
        inserted_by_document: dict[uuid.UUID, list[HawbJob]] = {}
        skip_notes_by_document: dict[uuid.UUID, list[str]] = {}
        for job_row in jobs_to_insert:
            existing = await db.scalar(select(HawbJob).where(HawbJob.hawb_number == job_row.hawb_number))
            if existing:
                is_merge_case = job_row.source_kind == "blind" or existing.source_kind == "blind"
                if is_merge_case:
                    if job_row.source_kind == "blind":
                        blind_data, plain_data = job_row.extracted_data, existing.extracted_data
                    else:
                        blind_data, plain_data = existing.extracted_data, job_row.extracted_data
                    try:
                        proposed_data = await asyncio.get_event_loop().run_in_executor(
                            None, hawb_extract.merge_blind_job, plain_data, blind_data, email_body
                        )
                    except Exception:
                        logger.error("HAWB pending-update merge failed for %s", job_row.hawb_number, exc_info=True)
                        proposed_data = job_row.extracted_data
                    reason = "blind_companion_merge"
                    note = f"Companion match found for existing HAWB, queued for review: {job_row.hawb_number}"
                else:
                    proposed_data = job_row.extracted_data
                    reason = "duplicate_resend"
                    note = f"Duplicate HAWB queued for review: {job_row.hawb_number}"

                db.add(HawbJobPendingUpdate(
                    job_id=existing.id,
                    source_document_id=job_row.document_id,
                    reason=reason,
                    proposed_data=proposed_data,
                ))
                skip_notes_by_document.setdefault(job_row.document_id, []).append(note)
                continue
            db.add(job_row)
            inserted_by_document.setdefault(job_row.document_id, []).append(job_row)

        all_docs = [doc for doc, _ in plain_entries] + [doc for doc, _ in blind_entries]
        for doc in all_docs:
            doc_jobs = inserted_by_document.get(doc.id, [])
            doc.job_count = len(doc_jobs)
            notes = skip_notes_by_document.get(doc.id)
            if notes:
                note = "; ".join(notes)
                doc.error_message = note if not doc.error_message else f"{doc.error_message}; {note}"

        # --- Manifest creation: one per document that ended up with jobs attached to it ---
        if inserted_by_document:
            await db.flush()
            for doc in all_docs:
                doc_jobs = inserted_by_document.get(doc.id)
                if not doc_jobs:
                    continue
                manifest = HawbManifest(
                    job_count=len(doc_jobs),
                    total_weight_kg=sum((j.weight_kg or 0) for j in doc_jobs),
                    created_by=None,
                    source_kind="blind" if any(j.source_kind == "blind" for j in doc_jobs) else "plain",
                )
                db.add(manifest)
                await db.flush()
                for sequence, hawb_job in enumerate(doc_jobs, start=1):
                    hawb_job.manifest_id = manifest.id
                    hawb_job.manifest_sequence = sequence

        await db.commit()


async def _extract_and_create_document(
    db, att: dict, *, source_kind: str,
    source_message_id: str | None, sender_email: str | None, subject: str | None, email_body: str | None,
):
    from app.database import AsyncSessionLocal
    from app.models.hawb import HawbDocument

    file_bytes = att.get("data") or b""
    filename = att.get("filename") or "document.pdf"
    key = f"{settings.s3_prefix}/hawb/{source_message_id or 'manual'}/{filename}"
    await upload_bytes(file_bytes, key, content_type="application/pdf")

    # Commit a "processing" row immediately, in its own short transaction, so the
    # UI's live-refresh shows the email arrived right away — the extraction call
    # below is a real AI call and can take several seconds on its own.
    async with AsyncSessionLocal() as pre_db:
        pending_doc = HawbDocument(
            source_message_id=source_message_id,
            sender_email=sender_email,
            subject=subject,
            filename=filename,
            storage_bucket=settings.s3_bucket or "horizon-dev",
            storage_key=key,
            job_count=0,
            status="processing",
            source_kind=source_kind,
            email_body_text=email_body,
        )
        pre_db.add(pending_doc)
        await pre_db.commit()
        doc_id = pending_doc.id

    extractor = hawb_extract.extract_blind_candidates if source_kind == "blind" else hawb_extract.extract_jobs
    error_message: str | None = None
    try:
        jobs_data = await asyncio.get_event_loop().run_in_executor(None, extractor, file_bytes, filename)
    except Exception as e:
        logger.error("HAWB extraction failed for %s", filename, exc_info=True)
        jobs_data = []
        error_message = f"Extraction failed: {e}"

    # Re-fetch into the batch's own session so job/manifest linkage below (job_count,
    # error_message, relationships) commits atomically with the rest of the batch.
    document = await db.get(HawbDocument, doc_id)
    document.processed_at = datetime.now(timezone.utc)
    document.status = "failed" if error_message else "processed"
    document.error_message = error_message
    return document, jobs_data


def _build_job_row(job: dict, *, document_id, blind_document_id, source_kind: str, status: str):
    from app.models.hawb import HawbJob

    hawb_number = (job.get("hawb_number") or "").strip()
    return HawbJob(
        document_id=document_id,
        blind_document_id=blind_document_id,
        source_kind=source_kind,
        status=status,
        ready_at=datetime.now(timezone.utc) if status == "ready_to_manifest" else None,
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
    )


def _parse_dt(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None
