from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db
from app.models.booking import Booking
from app.models.booking_read import BookingRead
from app.models.notification import Notification
from app.models.user import User
from app.schemas.notification import LatestUnreadBooking, NotificationOut, NotificationsListOut

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_model=NotificationsListOut)
async def list_notifications(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Notification)
        .where(Notification.user_id == current_user.id)
        .order_by(Notification.created_at.desc())
        .limit(50)
    )
    items = result.scalars().all()

    unread_bookings_result = await db.execute(
        select(func.count(Booking.id))
        .select_from(Booking)
        .outerjoin(
            BookingRead,
            and_(
                BookingRead.booking_id == Booking.id,
                BookingRead.user_id == current_user.id,
            ),
        )
        .where(
            or_(
                BookingRead.booking_id == None,
                BookingRead.read_at < Booking.updated_at,
            )
        )
    )
    unread_bookings = unread_bookings_result.scalar_one()

    latest_unread_booking = None
    if unread_bookings > 0:
        latest_res = await db.execute(
            select(Booking.id, Booking.subject)
            .select_from(Booking)
            .outerjoin(
                BookingRead,
                and_(
                    BookingRead.booking_id == Booking.id,
                    BookingRead.user_id == current_user.id,
                ),
            )
            .where(
                or_(
                    BookingRead.booking_id == None,
                    BookingRead.read_at < Booking.updated_at,
                )
            )
            .order_by(Booking.updated_at.desc())
            .limit(1)
        )
        row = latest_res.first()
        if row:
            latest_unread_booking = LatestUnreadBooking(id=row.id, subject=row.subject)

    return NotificationsListOut(
        items=[NotificationOut.model_validate(n) for n in items],
        unread_count=sum(1 for n in items if not n.is_read),
        unread_bookings=unread_bookings,
        latest_unread_booking=latest_unread_booking,
    )


# /read-all must come before /{notification_id}/read to avoid route conflict
@router.patch("/read-all")
async def mark_all_read(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await db.execute(
        update(Notification)
        .where(Notification.user_id == current_user.id, Notification.is_read == False)
        .values(is_read=True)
    )
    await db.commit()
    return {"ok": True}


@router.patch("/{notification_id}/read")
async def mark_read(
    notification_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    notif = await db.get(Notification, notification_id)
    if notif is None or notif.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Notification not found")
    notif.is_read = True
    await db.commit()
    return {"ok": True}


@router.delete("/{notification_id}", status_code=204)
async def delete_notification(
    notification_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    notif = await db.get(Notification, notification_id)
    if notif is None or notif.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Notification not found")
    await db.delete(notif)
    await db.commit()
