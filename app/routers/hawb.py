import asyncio
import csv
import io
import json
import math
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db
from app.models.hawb import HawbDocument, HawbJob, HawbJobPendingUpdate, HawbManifest
from app.models.user import User
from app.schemas.hawb import (
    ExportManifestRequest, ExportManifestResponse, ExportSystemResult,
    HawbDocumentOut, HawbJobDetailOut, HawbJobOut, HawbJobPageOut, HawbJobPendingUpdateOut, HawbJobUpdate,
    HawbManifestDetailOut, HawbManifestOut, ManifestReorder, ManifestUpdate,
)
from app.services import indigo_export, mytransport_export
from app.services.hawb_ingest import _parse_dt, retry_document_extraction
from app.storage import presigned_url

router = APIRouter(prefix="/hawb", tags=["hawb"])


@router.get("/jobs", response_model=HawbJobPageOut)
async def list_jobs(
    status: str | None = Query(None),
    source_kind: str | None = Query(None),
    search: str | None = Query(None),
    document_id: UUID | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = select(HawbJob).order_by(HawbJob.created_at.desc())

    if status:
        q = q.where(HawbJob.status == status)
    if source_kind:
        q = q.where(HawbJob.source_kind == source_kind)
    if document_id:
        q = q.where(HawbJob.document_id == document_id)
    if search:
        s = f"%{search}%"
        q = q.where(or_(
            HawbJob.hawb_number.ilike(s),
            HawbJob.shipper.ilike(s),
            HawbJob.consignee.ilike(s),
        ))

    total = await db.scalar(select(func.count()).select_from(q.subquery()))

    items_q = q.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(items_q)
    items = result.scalars().all()

    return HawbJobPageOut(
        items=[HawbJobOut.model_validate(j) for j in items],
        total=total or 0,
        page=page,
        page_size=page_size,
        total_pages=math.ceil((total or 0) / page_size) if total else 1,
    )


@router.get("/jobs/{job_id}", response_model=HawbJobDetailOut)
async def get_job(
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    job = await db.get(HawbJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    url = await presigned_url(job.document.storage_key)
    job_out = HawbJobOut.model_validate(job)
    if job.blind_document_id:
        job_out.blind_pdf_url = await presigned_url(job.blind_document.storage_key)
    return HawbJobDetailOut(
        **job_out.model_dump(),
        document=HawbDocumentOut.model_validate(job.document),
        pdf_url=url,
    )


@router.post("/jobs/{job_id}/approve", response_model=HawbJobOut)
async def approve_job(
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    job = await db.get(HawbJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.locked:
        raise HTTPException(status_code=409, detail="Manifest has been exported and is locked")
    if job.status != "pending_review":
        raise HTTPException(status_code=409, detail=f"Job is '{job.status}', not pending review")

    job.status = "ready_to_manifest"
    job.ready_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(job)
    return HawbJobOut.model_validate(job)


@router.patch("/jobs/{job_id}", response_model=HawbJobOut)
async def update_job(
    job_id: UUID,
    body: HawbJobUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    job = await db.get(HawbJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.locked:
        raise HTTPException(status_code=409, detail="Manifest has been exported and is locked")

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(job, field, value)

    await db.commit()
    await db.refresh(job)
    return HawbJobOut.model_validate(job)


_PENDING_UPDATE_FIELDS = [
    "shipper", "consignee", "collection_at", "delivery_at", "package_qty", "weight_kg",
    "dangerous_goods", "dangerous_goods_notes", "client_account", "package_sequence",
    "shipper_contact", "shipper_phone", "shipper_reference", "consignee_contact",
    "consignee_phone", "consignee_reference", "temperature_range", "dimensions",
    "volumetric_weight_kg", "declared_value", "declared_value_currency", "direction",
    "special_handling", "packages",
]
_PENDING_UPDATE_DATE_FIELDS = {"collection_at", "delivery_at"}


@router.get("/job-updates", response_model=list[HawbJobPendingUpdateOut])
async def list_job_updates(
    status: str = Query("pending"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """status="all" returns every pending/applied/dismissed record — used by
    the merge-history view. Any other value (default "pending") filters to
    an exact status match, as before."""
    q = select(HawbJobPendingUpdate)
    if status != "all":
        q = q.where(HawbJobPendingUpdate.status == status)
    q = q.order_by(HawbJobPendingUpdate.created_at.desc())
    result = await db.execute(q)
    return [HawbJobPendingUpdateOut.model_validate(u) for u in result.scalars().all()]


@router.post("/job-updates/{update_id}/apply", response_model=HawbJobOut)
async def apply_job_update(
    update_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    update = await db.get(HawbJobPendingUpdate, update_id)
    if not update:
        raise HTTPException(status_code=404, detail="Pending update not found")
    if update.status != "pending":
        raise HTTPException(status_code=409, detail=f"Update is already '{update.status}'")

    job = await db.get(HawbJob, update.job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    data = update.proposed_data
    for field in _PENDING_UPDATE_FIELDS:
        if field not in data:
            continue
        value = data[field]
        if field in _PENDING_UPDATE_DATE_FIELDS:
            value = _parse_dt(value)
        setattr(job, field, value)
    job.extracted_data = data

    if update.reason == "blind_companion_merge":
        source_doc = await db.get(HawbDocument, update.source_document_id)
        job.source_kind = "blind"
        if source_doc.source_kind == "blind":
            job.blind_document_id = source_doc.id
        else:
            # The plain companion just arrived — repoint the primary document to
            # it, and keep the job's old (previously MF-PCS-only) document as
            # the blind companion reference.
            job.blind_document_id = job.document_id
            job.document_id = source_doc.id

    if not job.locked:
        job.status = "pending_review"
        job.ready_at = None

    update.status = "applied"
    update.resolved_at = datetime.now(timezone.utc)

    if job.manifest_id:
        manifest_jobs = (await db.execute(
            select(HawbJob.weight_kg).where(HawbJob.manifest_id == job.manifest_id)
        )).scalars().all()
        manifest = await db.get(HawbManifest, job.manifest_id)
        manifest.total_weight_kg = sum((w or 0) for w in manifest_jobs)

    await db.commit()
    await db.refresh(job)
    return HawbJobOut.model_validate(job)


@router.post("/job-updates/{update_id}/dismiss", response_model=HawbJobPendingUpdateOut)
async def dismiss_job_update(
    update_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    update = await db.get(HawbJobPendingUpdate, update_id)
    if not update:
        raise HTTPException(status_code=404, detail="Pending update not found")
    if update.status != "pending":
        raise HTTPException(status_code=409, detail=f"Update is already '{update.status}'")

    update.status = "dismissed"
    update.resolved_at = datetime.now(timezone.utc)
    await db.commit()
    update = await db.get(HawbJobPendingUpdate, update.id, populate_existing=True)
    return HawbJobPendingUpdateOut.model_validate(update)


@router.get("/documents/processing", response_model=list[HawbDocumentOut])
async def list_processing_documents(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Documents whose extraction hasn't finished yet — lets the UI show an
    "email received, processing…" indicator before a manifest exists to show."""
    result = await db.execute(
        select(HawbDocument).where(HawbDocument.status == "processing").order_by(HawbDocument.received_at.desc())
    )
    return [HawbDocumentOut.model_validate(d) for d in result.scalars().all()]


@router.get("/manifests", response_model=list[HawbManifestOut])
async def list_manifests(
    source_kind: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = select(HawbManifest).order_by(HawbManifest.created_at.desc())
    if source_kind:
        q = q.where(HawbManifest.source_kind == source_kind)
    result = await db.execute(q)
    manifests = result.scalars().all()

    hawb_numbers_by_manifest: dict[UUID, list[str]] = {}
    remarks_by_manifest: dict[UUID, str] = {}
    if manifests:
        jobs_result = await db.execute(
            select(HawbJob.manifest_id, HawbJob.hawb_number)
            .where(HawbJob.manifest_id.in_([m.id for m in manifests]))
            .order_by(HawbJob.manifest_id, HawbJob.manifest_sequence)
        )
        for manifest_id, hawb_number in jobs_result.all():
            hawb_numbers_by_manifest.setdefault(manifest_id, []).append(hawb_number)

        doc_result = await db.execute(
            select(HawbDocument.manifest_id, HawbDocument.error_message)
            .where(HawbDocument.manifest_id.in_([m.id for m in manifests]))
        )
        for manifest_id, error_message in doc_result.all():
            if error_message:
                remarks_by_manifest[manifest_id] = error_message

    return [
        HawbManifestOut(
            **HawbManifestOut.model_validate(m).model_dump(exclude={"hawb_numbers", "remarks"}),
            hawb_numbers=hawb_numbers_by_manifest.get(m.id, []),
            remarks=remarks_by_manifest.get(m.id),
        )
        for m in manifests
    ]


@router.get("/manifests/{manifest_id}", response_model=HawbManifestDetailOut)
async def get_manifest(
    manifest_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    manifest = await db.get(HawbManifest, manifest_id)
    if not manifest:
        raise HTTPException(status_code=404, detail="Manifest not found")

    jobs_result = await db.execute(
        select(HawbJob).where(HawbJob.manifest_id == manifest_id).order_by(HawbJob.manifest_sequence)
    )
    jobs = jobs_result.scalars().all()
    if not jobs:
        # A manifest with zero jobs is a real data bug for any settled status —
        # but for a placeholder still extracting (or one that failed / had
        # nothing new to manifest, or was ignored before extraction ever ran),
        # it's expected: locate its source document via the reverse link
        # instead of jobs[0].document.
        if manifest.status not in ("extracting", "failed", "ignored"):
            raise HTTPException(status_code=404, detail="Manifest has no jobs")
        document = await db.scalar(select(HawbDocument).where(HawbDocument.manifest_id == manifest_id))
        if not document:
            raise HTTPException(status_code=404, detail="Manifest has no linked document")
        url = await presigned_url(document.storage_key)
        manifest_out = HawbManifestOut.model_validate(manifest)
        return HawbManifestDetailOut(
            **manifest_out.model_dump(exclude={"hawb_numbers", "remarks"}),
            hawb_numbers=[],
            remarks=document.error_message,
            jobs=[],
            document=HawbDocumentOut.model_validate(document),
            pdf_url=url,
        )

    # Every job in a manifest comes from the same source PDF, so any one of
    # them points at the document to show in the PDF pane.
    document = jobs[0].document
    url = await presigned_url(document.storage_key)

    jobs_out = []
    for j in jobs:
        j_out = HawbJobOut.model_validate(j)
        if j.blind_document_id:
            j_out.blind_pdf_url = await presigned_url(j.blind_document.storage_key)
        jobs_out.append(j_out)

    manifest_out = HawbManifestOut.model_validate(manifest)
    return HawbManifestDetailOut(
        **manifest_out.model_dump(exclude={"hawb_numbers", "remarks"}),
        hawb_numbers=[j.hawb_number for j in jobs],
        remarks=document.error_message,
        jobs=jobs_out,
        document=HawbDocumentOut.model_validate(document),
        pdf_url=url,
    )


@router.post("/manifests/{manifest_id}/retry-extraction", response_model=HawbManifestOut)
async def retry_manifest_extraction(
    manifest_id: UUID,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Re-run extraction for a failed plain-document manifest, reusing the PDF
    already in storage — no need for the sender to resend the email."""
    manifest = await db.get(HawbManifest, manifest_id)
    if not manifest:
        raise HTTPException(status_code=404, detail="Manifest not found")
    if manifest.status != "failed":
        raise HTTPException(status_code=409, detail=f"Manifest is '{manifest.status}', not failed")

    document = await db.scalar(select(HawbDocument).where(HawbDocument.manifest_id == manifest_id))
    if not document:
        raise HTTPException(status_code=409, detail="No source document linked to this manifest")

    manifest.status = "extracting"
    document.status = "processing"
    document.error_message = None
    await db.commit()

    background_tasks.add_task(retry_document_extraction, document.id)

    manifest = await db.get(HawbManifest, manifest.id, populate_existing=True)
    return HawbManifestOut.model_validate(manifest)


@router.patch("/manifests/{manifest_id}", response_model=HawbManifestOut)
async def update_manifest(
    manifest_id: UUID,
    body: ManifestUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    manifest = await db.get(HawbManifest, manifest_id)
    if not manifest:
        raise HTTPException(status_code=404, detail="Manifest not found")
    if manifest.status not in ("pending_review", "open"):
        raise HTTPException(status_code=409, detail=f"Manifest is '{manifest.status}' and locked")

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(manifest, field, value)

    await db.commit()
    manifest = await db.get(HawbManifest, manifest.id, populate_existing=True)
    return HawbManifestOut.model_validate(manifest)


@router.patch("/manifests/{manifest_id}/jobs/reorder", response_model=list[HawbJobOut])
async def reorder_manifest_jobs(
    manifest_id: UUID,
    body: ManifestReorder,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    manifest = await db.get(HawbManifest, manifest_id)
    if not manifest:
        raise HTTPException(status_code=404, detail="Manifest not found")
    if manifest.status not in ("pending_review", "open"):
        raise HTTPException(status_code=409, detail=f"Manifest is '{manifest.status}' and locked")

    result = await db.execute(select(HawbJob).where(HawbJob.manifest_id == manifest_id))
    jobs_by_id = {j.id: j for j in result.scalars().all()}

    if set(body.job_ids) != set(jobs_by_id.keys()):
        raise HTTPException(status_code=400, detail="job_ids must match exactly the jobs in this manifest")

    for sequence, job_id in enumerate(body.job_ids, start=1):
        jobs_by_id[job_id].manifest_sequence = sequence

    await db.commit()

    ordered_jobs = [jobs_by_id[job_id] for job_id in body.job_ids]
    for job in ordered_jobs:
        await db.refresh(job)
    return [HawbJobOut.model_validate(j) for j in ordered_jobs]


@router.post("/manifests/{manifest_id}/export")
async def export_manifest(
    manifest_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    manifest = await db.get(HawbManifest, manifest_id)
    if not manifest:
        raise HTTPException(status_code=404, detail="Manifest not found")
    if manifest.status != "open":
        raise HTTPException(status_code=409, detail=f"Manifest is '{manifest.status}', not open")
    if manifest.exported_at is not None:
        raise HTTPException(status_code=409, detail="Manifest has already been exported")

    missing_fields = [
        label for label, value in [
            ("Job reference", manifest.job_reference),
            ("Account number", manifest.account_number),
            ("Customer number", manifest.customer_number),
            ("Vehicle size", manifest.vehicle_size),
        ] if not value
    ]
    if missing_fields:
        raise HTTPException(
            status_code=409,
            detail=f"Missing required fields before export: {', '.join(missing_fields)}",
        )

    jobs_result = await db.execute(
        select(HawbJob).where(HawbJob.manifest_id == manifest_id).order_by(HawbJob.manifest_sequence)
    )
    jobs = jobs_result.scalars().all()

    pending = [j.hawb_number for j in jobs if j.status == "pending_review"]
    if pending:
        raise HTTPException(
            status_code=409,
            detail=f"Manifest has jobs still pending review: {', '.join(pending)}",
        )

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "HAWB", "Job Code", "Shipper", "Consignee", "Collection",
        "Weight (kg)", "Packages", "Temperature", "Dangerous Goods",
    ])
    for job in jobs:
        writer.writerow([
            job.hawb_number,
            job.client_account or "",
            job.shipper or "",
            job.consignee or "",
            job.collection_at.isoformat() if job.collection_at else "",
            job.weight_kg or "",
            job.package_qty or "",
            job.temperature_range or "",
            job.dangerous_goods_notes or "None",
        ])

    now = datetime.now(timezone.utc)
    for job in jobs:
        job.locked = True
        job.status = "manifested"
        job.manifested_at = now

    # Status intentionally stays 'open' — exported_at is what marks this manifest
    # (and its now-locked jobs) as exported, not a status transition.
    manifest.exported_at = now
    await db.commit()

    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{manifest.reference_number}.csv"'},
    )


async def _run_indigo_export(
    manifest: HawbManifest, payload: dict, already_booked: bool,
) -> ExportSystemResult:
    if already_booked:
        return ExportSystemResult(status="booked", reference=manifest.indigo_job_number)
    try:
        data = await indigo_export.call_indigo_addjob(payload, manifest.account_number)
    except indigo_export.IndigoRequestError as exc:
        return ExportSystemResult(status="failed", error=str(exc))
    # The whole manifest is one Indigo Job, so this is all-or-nothing per
    # system: either Indigo returns a JobNumber for it, or it doesn't and
    # this system's leg is 'failed' (independent of how mytransport does).
    results = data.get("Jobs", {}).get("Job", [])
    result = results[0] if results else {}
    job_number = result.get("JobNumber")
    if job_number:
        return ExportSystemResult(status="booked", reference=str(job_number))
    return ExportSystemResult(
        status="failed",
        error=result.get("Errormessage") or f"Indigo rejected the job: {json.dumps(result)[:500]}",
    )


async def _run_mytransport_export(
    manifest: HawbManifest, payload: dict, already_booked: bool,
) -> ExportSystemResult:
    if already_booked:
        return ExportSystemResult(
            status="booked", reference=manifest.mytransport_order_no,
            tracking_url=manifest.mytransport_tracking_url,
        )
    try:
        data = await mytransport_export.call_mytransport_import(payload)
    except mytransport_export.MytransportRequestError as exc:
        return ExportSystemResult(status="failed", error=str(exc))
    if not mytransport_export.is_success_response(data):
        return ExportSystemResult(status="failed", error=f"mytransport rejected the order: {json.dumps(data)[:500]}")
    result = data.get("result", {})
    order_nos = result.get("new_ordernos") or []
    order_no = str(order_nos[0]) if order_nos else None
    tracktrace = result.get("order_tracktrace") or {}
    tracking_url = tracktrace.get(order_no, {}).get("local_tracktrace_url") if order_no else None
    return ExportSystemResult(status="booked", reference=order_no, tracking_url=tracking_url)


@router.post("/manifests/{manifest_id}/carrier-export", response_model=ExportManifestResponse)
async def export_manifest_dual(
    manifest_id: UUID,
    body: ExportManifestRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Book this manifest into Indigo and mytransport/EasyTrans at once, both
    fired concurrently — the manifest's Start point is the pickup, with every
    HAWB stop riding along as its own drop/destination entry in each system's
    own shape. Runs server-side so neither system's login ever ships in the
    frontend bundle. Each system's outcome is tracked independently
    (indigo_export_status / mytransport_export_status): the manifest locks as
    soon as either books successfully, and a manifest with one system still
    'failed' can be re-posted here to retry only that one — the system that
    already booked is echoed back, never called twice. See Horizon-Web's
    docs/indigo-addjob-integration.md and docs/mytransport-export-integration.md."""
    manifest = await db.get(HawbManifest, manifest_id)
    if not manifest:
        raise HTTPException(status_code=404, detail="Manifest not found")
    if manifest.status not in ("open", "exported"):
        raise HTTPException(status_code=409, detail=f"Manifest is '{manifest.status}', not open")
    indigo_already_booked = manifest.indigo_export_status == "booked"
    mytransport_already_booked = manifest.mytransport_export_status == "booked"
    if manifest.status == "exported" and indigo_already_booked and mytransport_already_booked:
        raise HTTPException(status_code=409, detail="Manifest has already been exported to both Indigo and EasyTrans")

    if not manifest.start_point:
        raise HTTPException(status_code=409, detail="Missing required field before export: Start point")
    if not (manifest.customer_number or "").strip().isdigit():
        raise HTTPException(status_code=409, detail="Missing required field before export: Customer number")

    jobs_result = await db.execute(
        select(HawbJob).where(HawbJob.manifest_id == manifest_id).order_by(HawbJob.manifest_sequence)
    )
    jobs = jobs_result.scalars().all()
    if not jobs:
        raise HTTPException(status_code=409, detail="Manifest has no jobs to export")

    # Del/Coll decides which end of a HAWB is the stop the driver actually
    # makes, so a blank one has no address to book against — and defaulting it
    # would silently pick a country. Rejected here rather than guessed.
    missing_service = [
        j.hawb_number for j in jobs if j.job_service_type not in ("collection", "delivery")
    ]
    if missing_service:
        raise HTTPException(
            status_code=409,
            detail="Del/Coll must be set on every HAWB before export. Missing on: "
                   + ", ".join(missing_service),
        )

    # One destination/drop per merged stop, in the order the Merge run-order
    # view shows them — what the user merged and reordered on the manifest is
    # exactly what gets booked, in both systems.
    job_groups = mytransport_export.group_jobs_by_merge(list(jobs))
    conflicts = mytransport_export.validate_merge_groups(job_groups)
    if conflicts:
        raise HTTPException(status_code=409, detail=" ".join(conflicts))
    groups_only = [group for _, group in job_groups]
    mytransport_payload = mytransport_export.build_mytransport_order_payload(manifest, groups_only)
    indigo_payload = indigo_export.build_indigo_addjob_payload(manifest.service_type or "", manifest, groups_only)

    # mytransport requires at least one real stop besides the guaranteed
    # Start point (and the End point, when build_mytransport_order_payload is
    # booking it as its own closing destination too) — fewer means every job
    # group was skipped as a backhaul collection, confirmed live (errorno 30,
    # "A minimum of two destinations... is required") that EasyTrans rejects
    # a route with nothing real on it. Indigo has no equivalent minimum.
    closes_at_end_point = bool(mytransport_export.resolve_end_point(manifest))
    real_stop_count = len(mytransport_payload["orders"][0]["order_destinations"]) - 1 - (1 if closes_at_end_point else 0)
    if real_stop_count < 1:
        raise HTTPException(
            status_code=409,
            detail="Every HAWB on this manifest is a backhaul collection at the End point — "
                   "there's no real stop left to export.",
        )

    if body.dry_run:
        # Unlike Indigo (auth in a header), mytransport's login travels inside
        # the body itself — redact it before handing the payload back to
        # whichever authenticated staff member happened to call dry_run.
        redacted_mytransport = {
            **mytransport_payload,
            "authentication": {**mytransport_payload["authentication"], "password": "***"},
        }
        return ExportManifestResponse(
            indigo=ExportSystemResult(status="skipped"),
            mytransport=ExportSystemResult(status="skipped"),
            payloads={"indigo": indigo_payload, "mytransport": redacted_mytransport},
        )

    indigo_result, mytransport_result = await asyncio.gather(
        _run_indigo_export(manifest, indigo_payload, indigo_already_booked),
        _run_mytransport_export(manifest, mytransport_payload, mytransport_already_booked),
    )

    if indigo_result.status == "booked":
        manifest.indigo_job_number = indigo_result.reference
    manifest.indigo_export_status = indigo_result.status
    manifest.indigo_export_error = indigo_result.error

    if mytransport_result.status == "booked":
        manifest.mytransport_order_no = mytransport_result.reference
        manifest.mytransport_tracking_url = mytransport_result.tracking_url
    manifest.mytransport_export_status = mytransport_result.status
    manifest.mytransport_export_error = mytransport_result.error

    # The manifest locks as soon as either system has booked it — that leg is
    # live and can't be un-booked by leaving the manifest editable. A system
    # that's still 'failed' stays retryable (see already_booked above) without
    # touching the one that already went through.
    if (indigo_result.status == "booked" or mytransport_result.status == "booked") and manifest.status != "exported":
        now = datetime.now(timezone.utc)
        for job in jobs:
            job.locked = True
            job.status = "manifested"
            job.manifested_at = now
        manifest.status = "exported"
        manifest.exported_at = now

    await db.commit()

    return ExportManifestResponse(indigo=indigo_result, mytransport=mytransport_result)


@router.post("/manifests/{manifest_id}/cancel", response_model=HawbManifestOut)
async def cancel_manifest(
    manifest_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    manifest = await db.get(HawbManifest, manifest_id)
    if not manifest:
        raise HTTPException(status_code=404, detail="Manifest not found")
    if manifest.status != "open":
        raise HTTPException(status_code=409, detail=f"Manifest is '{manifest.status}', not open")
    if manifest.exported_at is not None:
        raise HTTPException(status_code=409, detail="Manifest has already been exported and cannot be cancelled")

    # Soft delete: jobs stay attached (not detached/released) and just get
    # locked, so Reopen can restore this manifest exactly as it was — nothing
    # to reconcile against jobs that might have been claimed elsewhere.
    jobs_result = await db.execute(select(HawbJob).where(HawbJob.manifest_id == manifest_id))
    for job in jobs_result.scalars().all():
        job.locked = True

    manifest.status = "cancelled"
    manifest.cancelled_at = datetime.now(timezone.utc)

    await db.commit()
    manifest = await db.get(HawbManifest, manifest.id, populate_existing=True)
    return HawbManifestOut.model_validate(manifest)


@router.post("/manifests/{manifest_id}/reopen", response_model=HawbManifestOut)
async def reopen_manifest(
    manifest_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    manifest = await db.get(HawbManifest, manifest_id)
    if not manifest:
        raise HTTPException(status_code=404, detail="Manifest not found")
    if manifest.status != "cancelled":
        raise HTTPException(status_code=409, detail=f"Manifest is '{manifest.status}', not cancelled")

    jobs_result = await db.execute(select(HawbJob).where(HawbJob.manifest_id == manifest_id))
    for job in jobs_result.scalars().all():
        job.locked = False

    manifest.status = "open"
    manifest.cancelled_at = None

    await db.commit()
    manifest = await db.get(HawbManifest, manifest.id, populate_existing=True)
    return HawbManifestOut.model_validate(manifest)


@router.post("/manifests/{manifest_id}/confirm", response_model=HawbManifestOut)
async def confirm_manifest(
    manifest_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    manifest = await db.get(HawbManifest, manifest_id)
    if not manifest:
        raise HTTPException(status_code=404, detail="Manifest not found")
    if manifest.status not in ("booked", "on_hold"):
        raise HTTPException(status_code=409, detail=f"Manifest is '{manifest.status}', not booked or on hold")

    manifest.status = "confirmed"
    await db.commit()
    manifest = await db.get(HawbManifest, manifest.id, populate_existing=True)
    return HawbManifestOut.model_validate(manifest)


@router.post("/manifests/{manifest_id}/hold", response_model=HawbManifestOut)
async def hold_manifest(
    manifest_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    manifest = await db.get(HawbManifest, manifest_id)
    if not manifest:
        raise HTTPException(status_code=404, detail="Manifest not found")
    if manifest.status not in ("booked", "confirmed"):
        raise HTTPException(status_code=409, detail=f"Manifest is '{manifest.status}', not booked or confirmed")

    manifest.status = "on_hold"
    await db.commit()
    manifest = await db.get(HawbManifest, manifest.id, populate_existing=True)
    return HawbManifestOut.model_validate(manifest)


@router.post("/manifests/{manifest_id}/mark-exported", response_model=HawbManifestOut)
async def mark_exported_manifest(
    manifest_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    manifest = await db.get(HawbManifest, manifest_id)
    if not manifest:
        raise HTTPException(status_code=404, detail="Manifest not found")
    if manifest.status != "confirmed":
        raise HTTPException(status_code=409, detail=f"Manifest is '{manifest.status}', not confirmed")

    manifest.status = "exported"
    manifest.exported_at = datetime.now(timezone.utc)
    await db.commit()
    manifest = await db.get(HawbManifest, manifest.id, populate_existing=True)
    return HawbManifestOut.model_validate(manifest)
