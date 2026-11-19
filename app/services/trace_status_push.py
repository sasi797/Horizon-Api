"""Report EasyTrans status changes on TRACE- manifests back to Trace.

Uses Trace's "Manifest Status Update API":
  1. POST {base}/login {email, password} -> JWT valid 4 hours (no refresh token)
  2. POST {base}/workflow/hooks/manifest-status-update
     Authorization: Bearer <token>   body {"request_no": "...", "status": "..."}

The EasyTrans webhook (routers/easytrans_webhook.py) saves each event, then
schedules push_event() for the manifest's latest status. The manifest is
matched on the trace_request_no Trace sent when it created it. The outcome is
kept on the event row (trace_push_*) so a push that never landed is visible.
"""

import asyncio
import base64
import json
import logging
import time
import uuid
from datetime import datetime, timezone

import httpx

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.hawb import EasytransWebhookEvent, HawbManifest
from app.services.thinv_auto_export import TRACE_REFERENCE_PREFIX

logger = logging.getLogger(__name__)

_LOGIN_PATH = "/login"
_UPDATE_PATH = "/workflow/hooks/manifest-status-update"
# Wait before each attempt, in seconds (Trace's guide: retry 5xx with backoff).
_RETRY_DELAYS = (0, 2, 4, 30)
_TIMEOUT = 15
# Re-login this long before the token's exp, so it never expires mid-call.
_TOKEN_MARGIN = 30 * 60
_TOKEN_FALLBACK_TTL = 3.5 * 3600

# EasyTrans status -> Trace status. Trace's guide shows "done" for a finished
# request; anything not mapped is sent as EasyTrans names it.
STATUS_MAP = {
    "finished": "done",
}

_token: str | None = None
_token_expires_at: float = 0.0
_token_lock = asyncio.Lock()


def is_enabled() -> bool:
    return bool(settings.trace_status_email and settings.trace_status_password)


def should_push(manifest: HawbManifest | None) -> bool:
    return (
        is_enabled()
        and manifest is not None
        and (manifest.reference_number or "").startswith(TRACE_REFERENCE_PREFIX)
        and bool(manifest.trace_request_no)
    )


def trace_status(easytrans_status: str | None) -> str | None:
    if not easytrans_status:
        return None
    return STATUS_MAP.get(easytrans_status, easytrans_status)


def _url(path: str) -> str:
    return settings.trace_status_base_url.rstrip("/") + path


def _token_exp(token: str) -> float | None:
    """The JWT's exp claim (Unix seconds), read without verifying — we only
    need to know when to log in again."""
    try:
        part = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return float(claims["exp"])
    except Exception:
        return None


async def _get_token(client: httpx.AsyncClient, *, force: bool = False) -> str:
    global _token, _token_expires_at
    async with _token_lock:
        if not force and _token and time.time() < _token_expires_at:
            return _token
        response = await client.post(_url(_LOGIN_PATH), json={
            "email": settings.trace_status_email,
            "password": settings.trace_status_password,
        })
        if not response.is_success:
            raise RuntimeError(f"Trace login failed: HTTP {response.status_code}: {response.text[:300]}")
        token = (response.json() or {}).get("token")
        if not token:
            raise RuntimeError("Trace login response has no token")
        exp = _token_exp(token)
        _token = token
        _token_expires_at = (exp - _TOKEN_MARGIN) if exp else time.time() + _TOKEN_FALLBACK_TTL
        return token


async def _record(event_id: uuid.UUID, **fields) -> None:
    try:
        async with AsyncSessionLocal() as db:
            event = await db.get(EasytransWebhookEvent, event_id)
            if event is None:
                return
            for name, value in fields.items():
                setattr(event, name, value)
            await db.commit()
    except Exception:
        logger.exception("Trace push: could not record outcome %s for event %s", fields, event_id)


async def push_event(event_id: uuid.UUID) -> None:
    """Background task: send one saved event's status to Trace. Re-logs in
    once on 401, retries network errors and 5xx/408/429. Never raises."""
    try:
        async with AsyncSessionLocal() as db:
            event = await db.get(EasytransWebhookEvent, event_id)
            manifest = await db.get(HawbManifest, event.manifest_id) if event and event.manifest_id else None
            if not should_push(manifest):
                return
            body = {"request_no": manifest.trace_request_no, "status": trace_status(event.status)}
            reference = manifest.reference_number
    except Exception:
        logger.exception("Trace push: could not load event %s", event_id)
        await _record(event_id, trace_push_status="failed", trace_push_error="Could not load event (see logs)")
        return

    if not body["status"]:
        await _record(event_id, trace_push_status="failed", trace_push_error="Event has no status")
        return

    error = None
    attempts = 0
    relogged = False
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for delay in _RETRY_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            attempts += 1
            try:
                token = await _get_token(client)
                response = await client.post(
                    _url(_UPDATE_PATH), json=body, headers={"Authorization": f"Bearer {token}"},
                )
                if response.status_code == 401 and not relogged:
                    relogged = True
                    token = await _get_token(client, force=True)
                    response = await client.post(
                        _url(_UPDATE_PATH), json=body, headers={"Authorization": f"Bearer {token}"},
                    )
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                error = f"{type(exc).__name__}: {exc}"
            else:
                if response.is_success:
                    logger.info(
                        "Trace push: %s request %s -> %s (HTTP %s)",
                        reference, body["request_no"], body["status"], response.status_code,
                    )
                    await _record(
                        event_id, trace_push_status="sent", trace_push_attempts=attempts,
                        trace_push_error=None, trace_pushed_at=datetime.now(timezone.utc),
                    )
                    return
                error = f"HTTP {response.status_code}: {response.text[:500]}"
                if response.status_code < 500 and response.status_code not in (408, 429):
                    break  # Trace rejected the request itself; resending won't change that.
            logger.warning("Trace push: attempt %s for event %s failed — %s", attempts, event_id, error)

    logger.error("Trace push: giving up on event %s (%s) after %s attempt(s) — %s", event_id, reference, attempts, error)
    await _record(event_id, trace_push_status="failed", trace_push_attempts=attempts, trace_push_error=error)
