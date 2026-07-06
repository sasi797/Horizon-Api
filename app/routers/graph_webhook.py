"""
Microsoft Graph change notification webhook endpoint.

Graph POSTs here whenever a new email arrives in the inbox, replacing the
30-second polling loop. Each notification triggers one _poll_inbox_async()
call which uses the existing dedup table to avoid reprocessing.
"""
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Query, Request, Response

from app.core.config import settings

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/graph")
async def graph_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    validationToken: Optional[str] = Query(None),
):
    # Validation handshake — Graph sends a POST with this query param when the
    # subscription is first created. Must echo it back as plain text within 10s.
    if validationToken:
        return Response(content=validationToken, media_type="text/plain", status_code=200)

    try:
        body = await request.json()
    except Exception:
        return Response(status_code=400)

    for notification in body.get("value", []):
        # Reject any notification that doesn't carry our secret — prevents spoofing.
        if notification.get("clientState") != settings.GRAPH_WEBHOOK_SECRET:
            continue
        if notification.get("changeType") == "created":
            background_tasks.add_task(_handle_new_mail)
            break  # One poll covers all notifications in this batch

    # Graph requires 202 — any other status triggers an automatic retry storm.
    return Response(status_code=202)


async def _handle_new_mail() -> None:
    """Trigger a full inbox poll when Graph notifies of a new message."""
    try:
        from app.tasks.tasks import _poll_inbox_async
        await _poll_inbox_async()
    except Exception as e:
        print(f"[BTS] Webhook-triggered poll error: {e}")
