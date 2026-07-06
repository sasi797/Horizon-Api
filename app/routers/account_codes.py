from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, or_, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db
from app.models.account_code import AccountCode
from app.models.user import User
from app.schemas.account_code import AccountCodeOut

router = APIRouter(prefix="/account-codes", tags=["account-codes"])


@router.get("", response_model=list[AccountCodeOut])
async def list_account_codes(
    q: str | None = Query(default=None, description="Search by code, name or site"),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    stmt = select(AccountCode).order_by(AccountCode.name)
    if q and q.strip():
        term = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                func.lower(AccountCode.code).like(func.lower(term)),
                func.lower(AccountCode.name).like(func.lower(term)),
                func.lower(AccountCode.site).like(func.lower(term)),
            )
        )
    result = await db.execute(stmt)
    return result.scalars().all()
