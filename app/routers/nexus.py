"""Create Nexus employees by driving its UI — see app/services/nexus_sync.py.

Nexus exposes no API to us, so the record is created the way a person would
create it: log in, open Settings -> Users, fill the New Employee form, submit.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, field_validator

from app.dependencies import get_current_user
from app.models.user import User
from app.services.nexus_sync import ROLE_OPTIONS, SHIFT_OPTIONS, NexusError, add_employee

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/nexus", tags=["nexus"])


class EmployeeRequest(BaseModel):
    full_name: str
    email: EmailStr
    password: str
    role: str = "Agent"
    shift: str = "No Shift"
    # Fill the form but stop before submitting, so a run can be checked
    # without creating a user.
    dry_run: bool = True
    headless: bool = True

    @field_validator("full_name", "password")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v.strip()

    @field_validator("role")
    @classmethod
    def known_role(cls, v: str) -> str:
        if v not in ROLE_OPTIONS:
            raise ValueError(f"must be one of {ROLE_OPTIONS}")
        return v

    @field_validator("shift")
    @classmethod
    def known_shift(cls, v: str) -> str:
        if v not in SHIFT_OPTIONS:
            raise ValueError(f"must be one of {SHIFT_OPTIONS}")
        return v


class PushResult(BaseModel):
    submitted: bool
    url: str
    screenshot: str


@router.get("/options")
async def nexus_options(current_user: User = Depends(get_current_user)):
    """The role and shift values Nexus actually offers.

    Served from the backend so the dropdowns cannot drift out of step with
    what the automation is willing to select.
    """
    return {"roles": ROLE_OPTIONS, "shifts": SHIFT_OPTIONS}


@router.post("/employees", response_model=PushResult)
async def create_employee(
    body: EmployeeRequest,
    current_user: User = Depends(get_current_user),
):
    """Create an employee in Nexus from the submitted values.

    Runs inline and takes 20-35 seconds. Acceptable at this volume; it moves
    to a Celery task before this is used in bulk.
    """
    logger.info(
        "Nexus employee push by %s: %s (dry_run=%s)", current_user.email, body.email, body.dry_run
    )
    try:
        result = await add_employee(
            {
                "full_name": body.full_name,
                "email": body.email,
                "password": body.password,
                "role": body.role,
                "shift": body.shift,
            },
            dry_run=body.dry_run,
            headless=body.headless,
        )
    except NexusError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    return PushResult(**result)
