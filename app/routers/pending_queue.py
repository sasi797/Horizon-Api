from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.dependencies import get_current_user, get_db
from app.models.booking import Booking
from app.models.pending_queue import PendingQueue
from app.models.user import User
from app.schemas.pending_queue import AssignRequest, PendingQueueOut

router = APIRouter(prefix="/pending-queue", tags=["pending-queue"])


@router.get("", response_model=list[PendingQueueOut])
async def list_pending(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    result = await db.execute(
        select(PendingQueue)
        .options(selectinload(PendingQueue.booking))
        .order_by(PendingQueue.pending_since.asc())
    )
    return result.scalars().all()


@router.post("/assign", response_model=dict)
async def assign_from_queue(
    body: AssignRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    pq_result = await db.execute(
        select(PendingQueue).where(PendingQueue.booking_id == body.booking_id)
    )
    pq = pq_result.scalar_one_or_none()
    if pq is None:
        raise HTTPException(status_code=404, detail="Not in pending queue")

    booking_result = await db.execute(
        select(Booking).where(Booking.id == body.booking_id)
    )
    booking = booking_result.scalar_one_or_none()
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found")

    booking.agent_id = body.agent_id
    booking.status = "In Progress"
    booking.assigned_at = datetime.now(timezone.utc)
    await db.delete(pq)
    await db.commit()
    return {"message": "Assigned successfully"}



@router.delete("/{booking_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_from_queue(
    booking_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    pq_result = await db.execute(select(PendingQueue).where(PendingQueue.booking_id == booking_id))
    pq = pq_result.scalar_one_or_none()
    if pq is None:
        raise HTTPException(status_code=404, detail="Not in pending queue")
    await db.delete(pq)
    await db.commit()
