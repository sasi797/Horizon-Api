import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db
from app.models.upload import Upload
from app.models.user import User
from app.schemas.upload import HistoryDetail, HistoryItem

router = APIRouter(prefix="/history", tags=["history"])


@router.get("", response_model=list[HistoryItem])
async def list_history(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Upload, User.name)
        .join(User, Upload.user_id == User.id)
        .order_by(Upload.uploaded_at.desc())
    )
    rows = result.all()
    return [
        HistoryItem(
            id=upload.id,
            filename=upload.filename,
            doc_type=upload.doc_type,
            uploaded_at=upload.uploaded_at,
            uploaded_by=name,
        )
        for upload, name in rows
    ]


@router.get("/{upload_id}", response_model=HistoryDetail)
async def get_upload(
    upload_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Upload, User.name)
        .join(User, Upload.user_id == User.id)
        .where(Upload.id == upload_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Upload not found")

    upload, name = row
    return HistoryDetail(
        id=upload.id,
        filename=upload.filename,
        doc_type=upload.doc_type,
        uploaded_at=upload.uploaded_at,
        uploaded_by=name,
        headers=upload.headers,
        rows=upload.rows,
    )
