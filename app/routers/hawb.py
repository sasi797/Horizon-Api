import csv
import io
import math
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db
from app.models.hawb import HawbDocument, HawbJob, HawbJobPendingUpdate, HawbManifest
from app.models.user import User
from app.schemas.hawb import (
    HawbDocumentOut, HawbJobDetailOut, HawbJobOut, HawbJobPageOut, HawbJobPendingUpdateOut, HawbJobUpdate,
    HawbManifestDetailOut, HawbManifestOut, ManifestReorder, ManifestUpdate,
)
from app.services.hawb_ingest import _parse_dt
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
    q = (
        select(HawbJobPendingUpdate)
        .where(HawbJobPendingUpdate.status == status)
        .order_by(HawbJobPendingUpdate.created_at.desc())
    )
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
    if manifests:
        jobs_result = await db.execute(
            select(HawbJob.manifest_id, HawbJob.hawb_number)
            .where(HawbJob.manifest_id.in_([m.id for m in manifests]))
            .order_by(HawbJob.manifest_id, HawbJob.manifest_sequence)
        )
        for manifest_id, hawb_number in jobs_result.all():
            hawb_numbers_by_manifest.setdefault(manifest_id, []).append(hawb_number)

    return [
        HawbManifestOut(
            **HawbManifestOut.model_validate(m).model_dump(exclude={"hawb_numbers"}),
            hawb_numbers=hawb_numbers_by_manifest.get(m.id, []),
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
        raise HTTPException(status_code=404, detail="Manifest has no jobs")

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
        **manifest_out.model_dump(exclude={"hawb_numbers"}),
        hawb_numbers=[j.hawb_number for j in jobs],
        jobs=jobs_out,
        document=HawbDocumentOut.model_validate(document),
        pdf_url=url,
    )


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

    manifest.status = "booked"
    await db.commit()

    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{manifest.reference_number}.csv"'},
    )


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
