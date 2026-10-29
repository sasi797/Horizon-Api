import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.dependencies import get_db
from app.models.hawb import HawbDocument, HawbJob, HawbManifest
from app.schemas.trace import TraceJobOut, TraceManifestIn, TraceManifestOut
from app.services.hawb_ingest import _build_job_row

router = APIRouter(prefix="/api/trace", tags=["trace"])

_DT_FORMAT = "%Y-%m-%dT%H:%M:%S"


async def require_trace_api_key(x_api_key: str | None = Header(None)) -> None:
    if not settings.trace_api_key:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Trace API is not enabled")
    if not x_api_key or not secrets.compare_digest(x_api_key, settings.trace_api_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")


def _document_key(external_ref: str) -> str:
    return f"trace:{external_ref}"


async def _manifest_out(db: AsyncSession, manifest: HawbManifest, external_ref: str, *, already_existed: bool) -> TraceManifestOut:
    jobs = (await db.execute(
        select(HawbJob).where(HawbJob.manifest_id == manifest.id).order_by(HawbJob.manifest_sequence)
    )).scalars().all()
    return TraceManifestOut(
        manifest_reference=manifest.reference_number,
        external_ref=external_ref,
        status=manifest.status,
        job_count=manifest.job_count,
        total_weight_kg=float(manifest.total_weight_kg),
        jobs=[TraceJobOut(hawb_number=j.hawb_number, sequence=j.manifest_sequence or 0) for j in jobs],
        already_existed=already_existed,
    )


@router.post(
    "/manifests",
    response_model=TraceManifestOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_trace_api_key)],
)
async def create_trace_manifest(
    body: TraceManifestIn,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Create one TRACE-nnnnnnn manifest holding every job in the payload.

    Idempotent on `external_ref`: re-sending a manifest that was already
    accepted returns it unchanged (HTTP 200, `already_existed: true`) instead
    of creating a second one."""
    doc_key = _document_key(body.external_ref)

    existing_doc = (await db.execute(
        select(HawbDocument).where(HawbDocument.filename == doc_key, HawbDocument.storage_bucket == "api")
    )).scalars().first()
    if existing_doc and existing_doc.manifest_id:
        manifest = await db.get(HawbManifest, existing_doc.manifest_id)
        response.status_code = status.HTTP_200_OK
        return await _manifest_out(db, manifest, body.external_ref, already_existed=True)

    hawb_numbers = [j.hawb_number for j in body.jobs]
    seen: set[str] = set()
    for number in hawb_numbers:
        if number in seen:
            raise HTTPException(status_code=422, detail=f"hawb_number '{number}' appears more than once in this payload")
        seen.add(number)

    clashes = (await db.execute(
        select(HawbJob.hawb_number).where(HawbJob.hawb_number.in_(hawb_numbers))
    )).scalars().all()
    if clashes:
        raise HTTPException(status_code=409, detail=f"hawb_number already exists: {', '.join(sorted(clashes))}")

    reference = (await db.execute(
        text("SELECT 'TRACE-' || lpad(nextval('trace_manifest_ref_seq')::text, 7, '0')")
    )).scalar_one()

    total_weight = sum(j.weight_kg for j in body.jobs)
    manifest = HawbManifest(
        reference_number=reference,
        job_count=len(body.jobs),
        total_weight_kg=total_weight,
        status="open",
        source_kind="plain",
        start_point=body.start_point,
        end_point=body.end_point,
        vehicle_size=body.vehicle_size,
        customer_number=body.customer_number,
        created_by=None,
    )
    db.add(manifest)
    await db.flush()

    # Trace sends structured data, not a PDF — but every job needs a parent
    # document row, so one is created per manifest to carry the raw payload.
    now = datetime.now(timezone.utc)
    document = HawbDocument(
        sender_email="trace-api",
        subject=f"Trace manifest {body.external_ref}",
        filename=doc_key,
        storage_bucket="api",
        storage_key=f"api/trace/{body.external_ref}",
        processed_at=now,
        job_count=len(body.jobs),
        status="processed",
        source_kind="plain",
        manifest_id=manifest.id,
    )
    db.add(document)
    await db.flush()

    for sequence, job_in in enumerate(body.jobs, start=1):
        data = job_in.model_dump(mode="json")
        data["shipper"] = job_in.shipper.to_blob()
        data["consignee"] = job_in.consignee.to_blob()
        data["collection_at"] = job_in.collection_at.strftime(_DT_FORMAT)
        data["delivery_at"] = job_in.delivery_at.strftime(_DT_FORMAT)
        data["extra"] = {"source_company": "trace", "external_ref": body.external_ref}
        job = _build_job_row(
            data, document_id=document.id, blind_document_id=None,
            source_kind="plain", status="ready_to_manifest",
        )
        job.job_service_type = job_in.job_service_type
        job.manifest_id = manifest.id
        job.manifest_sequence = sequence
        db.add(job)

    try:
        await db.commit()
    except IntegrityError:
        # A concurrent request with the same HAWB numbers won the race.
        await db.rollback()
        raise HTTPException(status_code=409, detail="One or more hawb_numbers already exist")

    await db.refresh(manifest)
    return await _manifest_out(db, manifest, body.external_ref, already_existed=False)
