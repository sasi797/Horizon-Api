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


# Anthropic's documented PDF limit for the Messages API — a plain attachment
# larger than this can never be extracted, so it's recorded as unprocessable
# up front rather than attempted and failing inside the API call.
MAX_PDF_SIZE_BYTES = 32 * 1024 * 1024


async def process_email_attachments(
    *, message_id: str, sender_email: str, subject: str, pdf_attachments: list[dict], body_text: str | None = None
) -> None:
    """Entry point called from the email poll loop for one inbound message.

    Despite the parameter name (kept for call-site stability), this is every
    non-inline attachment on the message, not just ones already confirmed to
    be usable PDFs — that classification happens in `ingest_email_batch`."""
    from app.database import AsyncSessionLocal
    from app.models.hawb import HawbProcessedEmail

    # Claim the message_id up front (insert, not just select) so two near-
    # simultaneous webhook deliveries for the same message can't both slip
    # past a check-then-insert-later race and double-ingest the same PDFs.
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            pg_insert(HawbProcessedEmail).values(message_id=message_id).on_conflict_do_nothing()
        )
        await db.commit()
        if result.rowcount == 0:
            return

    try:
        await ingest_email_batch(
            pdf_attachments,
            source_message_id=message_id, sender_email=sender_email, subject=subject, email_body=body_text,
        )
    except Exception:
        logger.error("HAWB batch ingest failed for message %s", message_id, exc_info=True)


async def ingest_email_batch(
    attachments: list[dict], *,
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

    Every plain document already has its manifest by the time this function
    runs — `_extract_and_create_document` creates it up front (status
    "extracting") so it's visible in the UI before extraction even starts.
    This function only ever updates that placeholder in place (to "open" with
    real totals, or "failed" if nothing new landed on it) — it never creates a
    manifest for a plain document. Blind documents are unaffected by this and
    keep getting a manifest created after the fact, only if unmatched jobs
    land on them, exactly as before this feature existed.

    A non-blind attachment that can never be extracted — not a PDF, empty
    content, or too large for Anthropic's API — still gets a manifest (status
    "failed" immediately, no "extracting" step, since the outcome is already
    known) so nothing lands in the inbox and vanishes without a trace. Blind
    (MF-PCS) attachments are exempt from this — an unusable blind attachment
    is silently skipped, exactly as before this feature existed.
    """
    from app.database import AsyncSessionLocal
    from app.models.hawb import HawbDocument, HawbManifest

    blind_atts = [a for a in attachments if _is_blind_filename(a.get("filename") or "")]
    non_blind_atts = [a for a in attachments if not _is_blind_filename(a.get("filename") or "")]

    plain_atts: list[dict] = []
    unprocessable_atts: list[tuple[dict, str]] = []
    for att in non_blind_atts:
        filename = att.get("filename") or ""
        data = att.get("data") or b""
        if not filename.lower().endswith(".pdf"):
            unprocessable_atts.append((att, f"'{filename}' is not a PDF file — HAWB extraction only supports PDF attachments."))
        elif not data:
            unprocessable_atts.append((att, f"'{filename}' could not be retrieved from the email (no content returned)."))
        elif len(data) > MAX_PDF_SIZE_BYTES:
            size_mb = len(data) / (1024 * 1024)
            unprocessable_atts.append((att, f"'{filename}' is {size_mb:.1f} MB, which exceeds the {MAX_PDF_SIZE_BYTES // (1024 * 1024)} MB maximum PDF size supported for extraction."))
        else:
            plain_atts.append(att)

    async with AsyncSessionLocal() as db:
        # --- Unprocessable attachments: no extraction attempt, outcome already known ---
        for att, reason in unprocessable_atts:
            await _record_unprocessable_attachment(
                db, att, reason,
                source_message_id=source_message_id, sender_email=sender_email, subject=subject, email_body=email_body,
            )

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
        jobs_to_insert = []

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

        inserted_by_document, skip_notes_by_document = await _dedupe_and_insert_jobs(db, jobs_to_insert, email_body)
        await db.flush()

        # --- Plain documents: reuse (never recreate) the placeholder manifest created up front ---
        for doc, _ in plain_entries:
            manifest = await db.get(HawbManifest, doc.manifest_id)
            await _apply_plain_extraction_outcome(
                doc, manifest, inserted_by_document.get(doc.id, []), skip_notes_by_document.get(doc.id, []),
            )

        # --- Blind documents: unchanged — own manifest only if unmatched jobs landed on them ---
        for doc, _ in blind_entries:
            doc_jobs = inserted_by_document.get(doc.id)
            notes = skip_notes_by_document.get(doc.id)
            doc.job_count = len(doc_jobs) if doc_jobs else 0
            if notes:
                note = "; ".join(notes)
                doc.error_message = note if not doc.error_message else f"{doc.error_message}; {note}"
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
            # doc_jobs accumulates in match/merge-phase order (blind-matched jobs
            # first, then leftover unmatched jobs), not page order — re-sort by
            # page_start so the default run order follows the source PDF.
            doc_jobs.sort(key=lambda j: j.page_start if j.page_start is not None else 0)
            for sequence, hawb_job in enumerate(doc_jobs, start=1):
                hawb_job.manifest_id = manifest.id
                hawb_job.manifest_sequence = sequence

        await db.commit()


async def _dedupe_and_insert_jobs(db, jobs_to_insert: list, email_body: str | None):
    """Dedup against existing hawb_numbers: insert new, or queue a pending update.

    A hawb_number that already has a job is never silently dropped and never
    auto-applied (even to an already-exported job) — it's queued for manual
    review. If either side is blind-sourced, the "duplicate" usually isn't
    one at all: it's the missing plain/MF-PCS companion for an existing job
    that was persisted unmatched, now arriving in a later email — that case
    gets a real merge (same as the in-email merge path) instead of a plain
    field diff.

    Returns (inserted_by_document, skip_notes_by_document), both keyed by
    HawbDocument id.
    """
    from app.models.hawb import HawbJob, HawbJobPendingUpdate

    inserted_by_document: dict[uuid.UUID, list] = {}
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

    return inserted_by_document, skip_notes_by_document


async def _apply_plain_extraction_outcome(document, manifest, doc_jobs: list, notes: list[str]) -> None:
    """Update a plain document + its (always-preexisting) placeholder manifest
    once extraction/dedup for that document has finished. Never creates a new
    manifest — plain documents already have one via `manifest_id`, created up
    front in `_extract_and_create_document` before extraction ever ran."""
    document.job_count = len(doc_jobs)
    if notes:
        note = "; ".join(notes)
        document.error_message = note if not document.error_message else f"{document.error_message}; {note}"

    if doc_jobs:
        manifest.job_count = len(doc_jobs)
        manifest.total_weight_kg = sum((j.weight_kg or 0) for j in doc_jobs)
        manifest.source_kind = "blind" if any(j.source_kind == "blind" for j in doc_jobs) else "plain"
        manifest.status = "open"
        doc_jobs.sort(key=lambda j: j.page_start if j.page_start is not None else 0)
        for sequence, hawb_job in enumerate(doc_jobs, start=1):
            hawb_job.manifest_id = manifest.id
            hawb_job.manifest_sequence = sequence
    else:
        # Extraction failed (document.status == "failed") OR every extracted HAWB
        # was a duplicate (document.status == "processed" but nothing new to
        # manifest) — both collapse to the same visible state by product
        # decision: simpler than a third status, and retrying is harmless
        # either way (it just re-runs the same idempotent extract+dedupe).
        manifest.status = "failed"
        manifest.job_count = 0
        manifest.total_weight_kg = 0.0


async def _record_unprocessable_attachment(
    db, att: dict, reason: str, *,
    source_message_id: str | None, sender_email: str | None, subject: str | None, email_body: str | None,
) -> None:
    """A non-blind attachment that can never be extracted (wrong file type, no
    content, too large) — no extraction attempt is made since the outcome is
    already known, so this skips straight to a failed manifest + document
    instead of going through the extracting-first two-phase commit."""
    from app.models.hawb import HawbDocument, HawbManifest

    file_bytes = att.get("data") or b""
    filename = att.get("filename") or "attachment"
    key = f"{settings.s3_prefix}/hawb/{source_message_id or 'manual'}/{filename}"
    await upload_bytes(file_bytes, key, content_type="application/octet-stream")

    manifest = HawbManifest(
        job_count=0, total_weight_kg=0.0, status="failed",
        source_kind="plain", created_by=None,
    )
    db.add(manifest)
    await db.flush()

    document = HawbDocument(
        source_message_id=source_message_id,
        sender_email=sender_email,
        subject=subject,
        filename=filename,
        storage_bucket=settings.s3_bucket or "horizon-dev",
        storage_key=key,
        job_count=0,
        status="failed",
        error_message=reason,
        source_kind="plain",
        email_body_text=email_body,
        manifest_id=manifest.id,
        processed_at=datetime.now(timezone.utc),
    )
    db.add(document)


async def _run_extraction_for_document(file_bytes: bytes, filename: str, source_kind: str):
    extractor = hawb_extract.extract_blind_candidates if source_kind == "blind" else hawb_extract.extract_jobs
    try:
        jobs_data = await asyncio.get_event_loop().run_in_executor(None, extractor, file_bytes, filename)
        return jobs_data, None
    except Exception as e:
        logger.error("HAWB extraction failed for %s", filename, exc_info=True)
        return [], f"Extraction failed: {e}"


async def _extract_and_create_document(
    db, att: dict, *, source_kind: str,
    source_message_id: str | None, sender_email: str | None, subject: str | None, email_body: str | None,
):
    from app.database import AsyncSessionLocal
    from app.models.hawb import HawbDocument, HawbManifest

    file_bytes = att.get("data") or b""
    filename = att.get("filename") or "document.pdf"
    key = f"{settings.s3_prefix}/hawb/{source_message_id or 'manual'}/{filename}"
    await upload_bytes(file_bytes, key, content_type="application/pdf")

    # Commit a "processing" row immediately, in its own short transaction, so the
    # UI's live-refresh shows the email arrived right away — the extraction call
    # below is a real AI call and can take several seconds on its own. Plain
    # attachments also get a placeholder manifest in this same transaction, so a
    # manifest row is visible in the UI before extraction even starts; blind
    # (MF-PCS) attachments never get one — they only ever appear after the fact,
    # once matched/merged, exactly as before this feature existed.
    async with AsyncSessionLocal() as pre_db:
        manifest_id = None
        if source_kind == "plain":
            placeholder_manifest = HawbManifest(
                job_count=0, total_weight_kg=0.0, status="extracting",
                source_kind="plain", created_by=None,
            )
            pre_db.add(placeholder_manifest)
            await pre_db.flush()
            manifest_id = placeholder_manifest.id

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
            manifest_id=manifest_id,
        )
        pre_db.add(pending_doc)
        await pre_db.commit()
        doc_id = pending_doc.id

    jobs_data, error_message = await _run_extraction_for_document(file_bytes, filename, source_kind)

    # Re-fetch into the batch's own session so job/manifest linkage below (job_count,
    # error_message, relationships) commits atomically with the rest of the batch.
    document = await db.get(HawbDocument, doc_id)
    document.processed_at = datetime.now(timezone.utc)
    document.status = "failed" if error_message else "processed"
    document.error_message = error_message
    return document, jobs_data


async def retry_document_extraction(document_id: uuid.UUID) -> None:
    """Re-run extraction for one already-stored plain PDF, in isolation from the
    rest of its original email. Scoped to this single document only — no
    cross-email blind/plain re-matching (that's a batch-time-only concern;
    blind attachments never get a placeholder or a retry in the first place).
    The caller (router) has already flipped the manifest/document to
    "extracting"/"processing" before scheduling this, so this function is
    free to run start-to-finish in one session."""
    from app.database import AsyncSessionLocal
    from app.models.hawb import HawbDocument, HawbManifest
    from app.storage import download_bytes

    async with AsyncSessionLocal() as db:
        document = await db.get(HawbDocument, document_id)
        manifest = await db.get(HawbManifest, document.manifest_id)

        try:
            file_bytes = await download_bytes(document.storage_key)
            jobs_data, error_message = await _run_extraction_for_document(file_bytes, document.filename, "plain")
        except Exception as e:
            logger.error("HAWB retry extraction failed for document %s", document_id, exc_info=True)
            jobs_data, error_message = [], f"Extraction failed: {e}"

        document.processed_at = datetime.now(timezone.utc)
        document.status = "failed" if error_message else "processed"
        document.error_message = error_message

        jobs_to_insert = [
            _build_job_row(job, document_id=document.id, blind_document_id=None,
                            source_kind="plain", status="ready_to_manifest")
            for job in jobs_data if (job.get("hawb_number") or "").strip()
        ]
        inserted_by_document, skip_notes_by_document = await _dedupe_and_insert_jobs(db, jobs_to_insert, document.email_body_text)
        await db.flush()
        await _apply_plain_extraction_outcome(
            document, manifest, inserted_by_document.get(document.id, []), skip_notes_by_document.get(document.id, []),
        )
        await db.commit()


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
