from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.session import get_db
from app.core.security import get_current_agent, require_roles
from app.services import booking_service
from app.models.models import Booking, AuditLog
from app.schemas.schemas import (
    BookingIngest, BookingOut, BookingListItem,
    AnalysisUpsert, AnalysisOut, TransportSubmitResponse, PaginatedResponse
)

router = APIRouter(prefix="/bookings", tags=["bookings"])


@router.get("", response_model=PaginatedResponse)
async def list_bookings(
    status: Optional[str] = Query(None),
    priority: Optional[str] = Query(None),
    sender_email: Optional[str] = Query(None),
    mine_only: bool = Query(False),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_agent=Depends(get_current_agent),
):
    agent_id = current_agent.id if mine_only else None
    bookings, total = await booking_service.get_bookings(
        db, agent_id=agent_id, status=status, priority=priority,
        sender_email=sender_email, page=page, page_size=page_size
    )
    import math
    return PaginatedResponse(
        items=[BookingListItem.model_validate(b) for b in bookings],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=math.ceil(total / page_size),
    )


@router.get("/{booking_id}", response_model=BookingOut)
async def get_booking(
    booking_id: str,
    db: AsyncSession = Depends(get_db),
    current_agent=Depends(get_current_agent),
):
    booking = await booking_service.get_booking_by_id(db, booking_id)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    return BookingOut.model_validate(booking)


@router.post("/ingest", status_code=201)
async def ingest_booking(
    payload: BookingIngest,
    db: AsyncSession = Depends(get_db),
    current_agent=Depends(require_roles("admin", "supervisor")),
):
    """Internal endpoint — called by the email poller task."""
    booking = await booking_service.ingest_booking(db, payload.model_dump())
    agent = await booking_service.allocate_booking(db, booking)
    return {
        "booking_id": booking.booking_id,
        "id": str(booking.id),
        "assigned_to": agent.agent_code if agent else None,
        "status": booking.status,
    }


@router.patch("/{booking_id}/analysis", response_model=AnalysisOut)
async def save_analysis(
    booking_id: str,
    payload: AnalysisUpsert,
    db: AsyncSession = Depends(get_db),
    current_agent=Depends(get_current_agent),
):
    booking = await booking_service.get_booking_by_id(db, booking_id)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking.assigned_agent_id != current_agent.id and current_agent.role not in ("supervisor", "admin"):
        raise HTTPException(status_code=403, detail="Not assigned to this booking")

    analysis = await booking_service.upsert_analysis(
        db, booking, current_agent, payload.model_dump(exclude_none=True)
    )
    return AnalysisOut.model_validate(analysis)


@router.post("/{booking_id}/submit-transport", response_model=TransportSubmitResponse)
async def submit_transport(
    booking_id: str,
    db: AsyncSession = Depends(get_db),
    current_agent=Depends(get_current_agent),
):
    booking = await booking_service.get_booking_by_id(db, booking_id)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    if not booking.analysis or booking.analysis.is_draft:
        raise HTTPException(status_code=400, detail="Analysis must be submitted before transport")

    try:
        docket = await booking_service.submit_to_transport(db, booking, current_agent)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    # Trigger confirmation email async
    from app.tasks.tasks import send_confirmation_email
    send_confirmation_email.delay(str(booking.id))

    return TransportSubmitResponse(
        booking_id=booking.booking_id,
        docket_number=docket,
        status="submitted",
        message="Booking submitted to transport. Confirmation email will be sent.",
    )


@router.get("/{booking_id}/audit")
async def get_booking_audit(
    booking_id: str,
    db: AsyncSession = Depends(get_db),
    current_agent=Depends(get_current_agent),
):
    booking = await booking_service.get_booking_by_id(db, booking_id)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    result = await db.execute(
        select(AuditLog)
        .where(AuditLog.entity_type == "booking", AuditLog.entity_id == booking.id)
        .order_by(AuditLog.created_at)
    )
    logs = result.scalars().all()
    return [
        {
            "event": log.event,
            "actor_id": str(log.actor_id) if log.actor_id else None,
            "payload": log.payload,
            "timestamp": log.created_at.isoformat(),
        }
        for log in logs
    ]
