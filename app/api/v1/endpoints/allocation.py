from datetime import date
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.session import get_db
from app.core.security import get_current_agent, require_roles
from app.services import allocation_service
from app.services.agent_service import get_agent_by_id
from app.services.booking_service import get_booking_by_id
from app.schemas.schemas import AllocationStateOut, AllocationLogOut, ManualAssignRequest, PaginatedResponse
import math

router = APIRouter(prefix="/allocation", tags=["allocation"])


@router.get("/state")
async def get_state(
    db: AsyncSession = Depends(get_db),
    current_agent=Depends(get_current_agent),
):
    state = await allocation_service.get_allocation_state(db)
    return {
        "pointer": state["pointer"],
        "pool_size": state["pool_size"],
        "next_agent": state["next_agent"].agent_code if state["next_agent"] else None,
        "active_pool": [
            {"id": str(a.id), "agent_code": a.agent_code, "full_name": a.full_name, "initials": a.initials}
            for a in state["active_pool"]
        ],
    }


@router.get("/log", response_model=PaginatedResponse)
async def get_log(
    agent_id: Optional[str] = Query(None),
    shift_id: Optional[str] = Query(None),
    log_date: Optional[date] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_agent=Depends(get_current_agent),
):
    from uuid import UUID
    logs, total = await allocation_service.get_allocation_log(
        db,
        agent_id=UUID(agent_id) if agent_id else None,
        shift_id=UUID(shift_id) if shift_id else None,
        log_date=log_date,
        page=page,
        page_size=page_size,
    )
    return PaginatedResponse(
        items=[AllocationLogOut.model_validate(l) for l in logs],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=math.ceil(total / page_size),
    )


@router.get("/pending")
async def get_pending(
    db: AsyncSession = Depends(get_db),
    current_agent=Depends(require_roles("supervisor", "admin")),
):
    bookings = await allocation_service.get_pending_bookings(db)
    return [
        {
            "id": str(b.id),
            "booking_id": b.booking_id,
            "sender_email": b.sender_email,
            "email_subject": b.email_subject,
            "priority": b.priority,
            "received_at": b.received_at.isoformat(),
            "sla_deadline_at": b.sla_deadline_at.isoformat() if b.sla_deadline_at else None,
        }
        for b in bookings
    ]


@router.post("/pending/{booking_id}/assign")
async def manual_assign(
    booking_id: str,
    payload: ManualAssignRequest,
    db: AsyncSession = Depends(get_db),
    current_agent=Depends(require_roles("supervisor", "admin")),
):
    booking = await get_booking_by_id(db, booking_id)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    agent = await get_agent_by_id(db, str(payload.agent_id))
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    log = await allocation_service.manual_assign(db, booking, agent, current_agent)
    return {
        "booking_id": booking.booking_id,
        "assigned_to": agent.agent_code,
        "method": "manual_supervisor",
    }
