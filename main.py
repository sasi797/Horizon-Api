import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.formparsers import MultiPartParser

# Raise Starlette's default 1 MB cap on individual form fields to support
# large body_html payloads (e.g. replies containing pasted images).
MultiPartParser.max_part_size = 25 * 1024 * 1024  # 25 MB

from app.config import settings
from app.redis_client import close_redis, get_redis
from app.routers import (
    agents, allocations, attendance, auth, booking_config, bookings,
    dashboard, email_messages, email_templates, events, graph_webhook,
    hawb, history, notifications, pending_queue, reports, roles, shifts, account_codes,
    upload, users,
)


async def _subscription_renewal_loop():
    """Renew the Graph webhook subscription every 2 days (subscriptions expire after 3 days)."""
    await asyncio.sleep(2 * 24 * 3600)
    while True:
        try:
            from app.services.graph_subscription import renew_subscription
            await renew_subscription()
        except Exception as e:
            print(f"[BTS] Subscription renewal error: {e}")
        await asyncio.sleep(2 * 24 * 3600)


async def _sent_items_poll_loop():
    """Fallback poll every 5 minutes — catches sent items and any missed webhook notifications."""
    await asyncio.sleep(60)  # Let uvicorn finish binding before first run
    while True:
        try:
            from app.tasks.tasks import _poll_inbox_async
            await _poll_inbox_async()
        except Exception as e:
            print(f"[BTS] Fallback poll error: {e}")
        await asyncio.sleep(5 * 60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_redis()

    from app.services.graph_subscription import register_subscription
    await register_subscription()

    renewal_task = asyncio.create_task(_subscription_renewal_loop())
    poll_task = asyncio.create_task(_sent_items_poll_loop())

    yield

    renewal_task.cancel()
    poll_task.cancel()
    await close_redis()


app = FastAPI(
    title="BTS — Bookings to Ticket System API",
    version="1.0.0",
    description="Backend API for BTS frontend. Handles bookings, agents, attendance, allocation, and reports.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(bookings.router)
app.include_router(booking_config.router)
app.include_router(agents.router)
app.include_router(attendance.router)
app.include_router(allocations.router)
app.include_router(pending_queue.router)
app.include_router(roles.router)
app.include_router(users.router)
app.include_router(email_templates.router)
app.include_router(shifts.router)
app.include_router(reports.router)
app.include_router(dashboard.router)
app.include_router(email_messages.router)
app.include_router(notifications.router)
app.include_router(events.router)
app.include_router(account_codes.router)
app.include_router(graph_webhook.router)
app.include_router(upload.router)
app.include_router(history.router)
app.include_router(hawb.router)


@app.get("/health", tags=["health"])
async def health():
    return {"status": "ok", "version": "1.0.0"}
