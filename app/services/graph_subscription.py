"""
Manages the Microsoft Graph change notification subscription for the inbox.

Graph will POST to /webhooks/graph whenever a new message arrives, replacing
the 30-second polling loop. Subscriptions expire after 3 days maximum for mail
resources, so a renewal task runs every 2 days.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import httpx

from app.core.config import settings
from app.tasks.oauth2 import get_graph_token

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

_subscription_id: str | None = None


def _expiry_timestamp() -> str:
    return (
        datetime.now(timezone.utc) + timedelta(days=2, hours=23)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


async def register_subscription() -> str | None:
    """Register a Graph inbox subscription. Returns the subscription ID or None on failure."""
    global _subscription_id

    if not settings.WEBHOOK_BASE_URL:
        print("[BTS] WEBHOOK_BASE_URL not set — Graph webhook skipped, falling back to polling.")
        return None

    try:
        token = await asyncio.to_thread(get_graph_token, settings)
        notification_url = f"{settings.WEBHOOK_BASE_URL.rstrip('/')}/webhooks/graph"

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{GRAPH_BASE}/subscriptions",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={
                    "changeType": "created",
                    "notificationUrl": notification_url,
                    "resource": f"users/{settings.MAILBOX_EMAIL}/mailFolders/inbox/messages",
                    "expirationDateTime": _expiry_timestamp(),
                    "clientState": settings.GRAPH_WEBHOOK_SECRET,
                },
            )

        if resp.status_code == 201:
            _subscription_id = resp.json()["id"]
            print(f"[BTS] Graph subscription registered: {_subscription_id}")
            return _subscription_id

        print(f"[BTS] Subscription registration failed {resp.status_code}: {resp.text[:300]}")
        return None

    except Exception as e:
        print(f"[BTS] Subscription registration error: {e}")
        return None


async def renew_subscription() -> bool:
    """Extend the subscription expiry by another ~3 days. Re-registers if subscription is lost."""
    global _subscription_id

    if not _subscription_id:
        new_id = await register_subscription()
        return new_id is not None

    try:
        token = await asyncio.to_thread(get_graph_token, settings)

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.patch(
                f"{GRAPH_BASE}/subscriptions/{_subscription_id}",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"expirationDateTime": _expiry_timestamp()},
            )

        if resp.status_code == 200:
            print(f"[BTS] Graph subscription renewed: {_subscription_id}")
            return True

        print(f"[BTS] Subscription renewal failed {resp.status_code} — re-registering")
        _subscription_id = None
        new_id = await register_subscription()
        return new_id is not None

    except Exception as e:
        print(f"[BTS] Subscription renewal error: {e}")
        return False
