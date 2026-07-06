from fastapi import APIRouter
from app.api.v1.endpoints import auth, bookings, agents, shifts, attendance, allocation, reports

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(bookings.router)
api_router.include_router(agents.router)
api_router.include_router(shifts.router)
api_router.include_router(attendance.router)
api_router.include_router(allocation.router)
api_router.include_router(reports.router)
