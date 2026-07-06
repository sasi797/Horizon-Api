import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db
from app.models.upload import Upload
from app.models.user import User
from app.schemas.upload import UploadResponse
from app.services import claude_extract

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/upload", tags=["upload"])


@router.post("", response_model=UploadResponse)
async def upload_pdf(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ext = "." + file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in claude_extract.ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type. Allowed: {', '.join(claude_extract.ALLOWED_EXTENSIONS)}",
        )

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="Empty file")

    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, claude_extract.extract, file_bytes, file.filename
        )
    except Exception as e:
        logger.error("Extraction failed for %s", file.filename, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Extraction failed: {str(e)}")

    record = Upload(
        user_id=current_user.id,
        filename=file.filename,
        doc_type=result["doc_type"],
        headers=result["headers"],
        rows=result["rows"],
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)

    return UploadResponse(
        id=record.id,
        filename=record.filename,
        doc_type=record.doc_type,
        headers=record.headers,
        rows=record.rows,
    )
