import math
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db
from app.models.hawb import HawbDocument, HawbJob, HawbManifest
from app.models.user import User
from app.schemas.hawb import (
    HawbDocumentOut, HawbJobDetailOut, HawbJobOut, HawbJobPageOut, HawbJobUpdate,
    HawbManifestDetailOut, HawbManifestOut, ManifestCreate,
)
from app.storage import presigned_url

router = APIRouter(prefix="/hawb", tags=["hawb"])


@router.get("/jobs", response_model=HawbJobPageOut)
async def list_jobs(
    status: str | None = Query(None),
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
    return HawbJobDetailOut(
        **job_out.model_dump(),
        document=HawbDocumentOut.model_validate(job.document),
        pdf_url=url,
    )


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
        raise HTTPException(status_code=409, detail="Job is locked in a manifest — remove it from the manifest to edit")

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(job, field, value)

    await db.commit()
    await db.refresh(job)
    return HawbJobOut.model_validate(job)


@router.post("/jobs/{job_id}/ready", response_model=HawbJobOut)
async def mark_job_ready(
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    job = await db.get(HawbJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.locked or job.status == "manifested":
        raise HTTPException(status_code=409, detail="Job is already manifested")

    job.status = "ready_to_manifest"
    job.ready_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(job)
    return HawbJobOut.model_validate(job)


@router.get("/manifests", response_model=list[HawbManifestOut])
async def list_manifests(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(HawbManifest).order_by(HawbManifest.created_at.desc()))
    return [HawbManifestOut.model_validate(m) for m in result.scalars().all()]


@router.get("/manifests/{manifest_id}", response_model=HawbManifestDetailOut)
async def get_manifest(
    manifest_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    manifest = await db.get(HawbManifest, manifest_id)
    if not manifest:
        raise HTTPException(status_code=404, detail="Manifest not found")

    jobs_result = await db.execute(select(HawbJob).where(HawbJob.manifest_id == manifest_id))
    jobs = jobs_result.scalars().all()

    manifest_out = HawbManifestOut.model_validate(manifest)
    return HawbManifestDetailOut(
        **manifest_out.model_dump(),
        jobs=[HawbJobOut.model_validate(j) for j in jobs],
    )


@router.post("/manifests", response_model=HawbManifestDetailOut)
async def create_manifest(
    body: ManifestCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not body.job_ids:
        raise HTTPException(status_code=400, detail="job_ids must not be empty")

    result = await db.execute(select(HawbJob).where(HawbJob.id.in_(body.job_ids)))
    jobs = result.scalars().all()

    if len(jobs) != len(set(body.job_ids)):
        raise HTTPException(status_code=404, detail="One or more jobs not found")
    for job in jobs:
        if job.status != "ready_to_manifest" or job.locked:
            raise HTTPException(status_code=409, detail=f"Job {job.hawb_number} is not ready to manifest")

    total_weight = sum((job.weight_kg or 0) for job in jobs)

    manifest = HawbManifest(
        job_count=len(jobs),
        total_weight_kg=total_weight,
        created_by=current_user.id,
    )
    db.add(manifest)
    await db.flush()

    now = datetime.now(timezone.utc)
    for job in jobs:
        job.manifest_id = manifest.id
        job.status = "manifested"
        job.locked = True
        job.manifested_at = now

    await db.commit()
    await db.refresh(manifest)

    manifest_out = HawbManifestOut.model_validate(manifest)
    return HawbManifestDetailOut(
        **manifest_out.model_dump(),
        jobs=[HawbJobOut.model_validate(j) for j in jobs],
    )


@router.post("/manifests/{manifest_id}/jobs/{job_id}/remove", response_model=HawbJobOut)
async def remove_job_from_manifest(
    manifest_id: UUID,
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    job = await db.get(HawbJob, job_id)
    if not job or job.manifest_id != manifest_id:
        raise HTTPException(status_code=404, detail="Job not found in this manifest")

    job.manifest_id = None
    job.locked = False
    job.status = "ready_to_manifest"
    job.manifested_at = None

    manifest = await db.get(HawbManifest, manifest_id)
    if manifest:
        remaining_result = await db.execute(select(HawbJob).where(HawbJob.manifest_id == manifest_id))
        remaining_jobs = remaining_result.scalars().all()
        manifest.job_count = len(remaining_jobs)
        manifest.total_weight_kg = sum((j.weight_kg or 0) for j in remaining_jobs)

    await db.commit()
    await db.refresh(job)
    return HawbJobOut.model_validate(job)
