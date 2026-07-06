from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.core.security import (
    verify_password, create_access_token, create_refresh_token,
    decode_token, get_current_agent
)
from app.services.agent_service import get_agent_by_email
from app.services.attendance_service import mark_attendance
from app.schemas.schemas import LoginRequest, RefreshRequest, TokenResponse

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)):
    agent = await get_agent_by_email(db, payload.email)
    if not agent or not verify_password(payload.password, agent.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not agent.is_active:
        raise HTTPException(status_code=403, detail="Account inactive")

    # Auto-mark present on login
    try:
        await mark_attendance(db, agent.id, agent.shift_id, "present", agent.id)
    except Exception:
        pass  # Don't fail login if attendance mark fails

    token_data = {"sub": str(agent.id), "role": agent.role, "shift": str(agent.shift_id)}
    return TokenResponse(
        access_token=create_access_token(token_data),
        refresh_token=create_refresh_token(token_data),
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(payload: RefreshRequest, db: AsyncSession = Depends(get_db)):
    decoded = decode_token(payload.refresh_token)
    if decoded.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid token type")

    agent = await get_agent_by_email(db, decoded.get("sub", ""))
    if not agent:
        # Try by ID
        from app.services.agent_service import get_agent_by_id
        agent = await get_agent_by_id(db, decoded.get("sub", ""))
    if not agent:
        raise HTTPException(status_code=401, detail="Agent not found")

    token_data = {"sub": str(agent.id), "role": agent.role}
    return TokenResponse(
        access_token=create_access_token(token_data),
        refresh_token=create_refresh_token(token_data),
    )


@router.post("/logout")
async def logout(current_agent=Depends(get_current_agent)):
    # Stateless JWT — client drops tokens; add token blocklist here if needed
    return {"message": "Logged out"}


@router.get("/me")
async def me(current_agent=Depends(get_current_agent)):
    from app.schemas.schemas import AgentOut
    return AgentOut.model_validate(current_agent)
