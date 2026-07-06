from datetime import date
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.core.security import get_current_agent, require_roles
from app.services import attendance_service
from app.schemas.schemas import AttendanceSupervisorMark, AttendanceRecordOut

router = APIRouter(prefix="/attendance", tags=["attendance"])


@router.get("/today")
async def today_all_shifts(
    db: AsyncSession = Depends(get_db),
    current_agent=Depends(get_current_agent),
):
    summary = await attendance_service.build_daily_summary(db, date.today())
    return summary


@router.get("/today/{shift_id}")
async def today_by_shift(
    shift_id: str,
    db: AsyncSession = Depends(get_db),
    current_agent=Depends(get_current_agent),
):
    from uuid import UUID
    records = await attendance_service.get_today_attendance(db, shift_id=UUID(shift_id))
    return [AttendanceRecordOut.model_validate(r) for r in records]


@router.post("/mark", response_model=AttendanceRecordOut)
async def self_mark_present(
    db: AsyncSession = Depends(get_db),
    current_agent=Depends(get_current_agent),
):
    """Agent marks themselves present — also called automatically on login."""
    record = await attendance_service.mark_attendance(
        db,
        agent_id=current_agent.id,
        shift_id=current_agent.shift_id,
        status="present",
        marked_by_id=current_agent.id,
    )
    return AttendanceRecordOut.model_validate(record)


@router.patch("/{record_id}/status", response_model=AttendanceRecordOut)
async def supervisor_update_status(
    record_id: str,
    payload: AttendanceSupervisorMark,
    db: AsyncSession = Depends(get_db),
    current_agent=Depends(require_roles("supervisor", "admin")),
):
    """Supervisor overrides any agent's attendance status."""
    from sqlalchemy import select
    from app.models.models import AttendanceRecord, Agent
    from uuid import UUID

    # Get the agent from the payload
    agent_result = await db.execute(
        select(Agent).where(Agent.id == payload.agent_id)
    )
    agent = agent_result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    valid_statuses = ["present", "absent", "late", "on_break", "left_early"]
    if payload.status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {valid_statuses}")

    record = await attendance_service.mark_attendance(
        db,
        agent_id=agent.id,
        shift_id=agent.shift_id,
        status=payload.status,
        marked_by_id=current_agent.id,
        shift_date=payload.shift_date,
    )
    return AttendanceRecordOut.model_validate(record)


@router.get("/history")
async def attendance_history(
    agent_id: Optional[str] = Query(None),
    start_date: date = Query(...),
    end_date: date = Query(...),
    db: AsyncSession = Depends(get_db),
    current_agent=Depends(get_current_agent),
):
    from uuid import UUID
    target_id = UUID(agent_id) if agent_id else current_agent.id
    if target_id != current_agent.id and current_agent.role not in ("supervisor", "admin"):
        raise HTTPException(status_code=403, detail="Cannot view another agent's history")

    records = await attendance_service.get_attendance_history(db, target_id, start_date, end_date)
    return [AttendanceRecordOut.model_validate(r) for r in records]
