"""EasyTrans (mytransport.co.uk) status + POD webhook.

EasyTrans calls this once per order event — in-progress, collected, finished,
status update (new substatus) — see chapter 8 of "EasyTrans - JSON order
import v5.4.pdf". Registered with EasyTrans by emailing them the public URL:
https://split.api.spl-unboxed.com/webhooks/easytrans

Response rules from the spec (8.4) shape everything below:
  * 200 = processed. Anything else shows the response body to the driver who
    is signing off the stop, and EasyTrans never retries. So only a bad API
    key gets a non-200; an order number we don't know, or a failed POD
    upload, is still logged and answered 200 — never block a driver over it.
"""

import base64
import binascii
import copy
import logging
import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.dependencies import get_db
from app.models.hawb import EasytransWebhookEvent, HawbManifest
from app.storage import upload_bytes

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

_POD_PREFIX = "easytrans-pod"
_CONTENT_TYPES = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
}


def _parse_event_time(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _content_type(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return _CONTENT_TYPES.get(ext, "application/octet-stream")


def _safe_name(name: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "._-" else "_" for c in name).strip("._")
    return cleaned or "document"


async def _store_blob(encoded: str, key: str, content_type: str) -> str | None:
    """Upload one base64 blob to S3 and return its key, or None on failure
    (the caller then keeps the base64 in the payload so nothing is lost)."""
    try:
        data = base64.b64decode(encoded, validate=False)
    except (binascii.Error, ValueError):
        logger.warning("EasyTrans webhook: undecodable base64 for %s", key)
        return None
    try:
        await upload_bytes(data, key, content_type)
    except Exception:
        logger.exception("EasyTrans webhook: POD upload to S3 failed for %s", key)
        return None
    return key


async def _offload_pod(payload: dict, order_no: str, event_id: uuid.UUID) -> dict:
    """Returns a copy of `payload` where each destination's signature and POD
    documents are uploaded to S3 and replaced by `storage_key`, so the events
    table holds keys instead of multi-MB base64 strings."""
    stored = copy.deepcopy(payload)
    destinations = (stored.get("order") or {}).get("destinations") or []
    for index, destination in enumerate(destinations, start=1):
        result = destination.get("taskResult") if isinstance(destination, dict) else None
        if not isinstance(result, dict):
            continue
        stop = destination.get("stopNo") or index
        base = f"{_POD_PREFIX}/{order_no}/{event_id}/stop{stop}"

        signature = result.get("base64EncodedSignature")
        if signature:
            key = await _store_blob(signature, f"{base}-signature.png", "image/png")
            if key:
                result.pop("base64EncodedSignature")
                result["signatureStorageKey"] = key

        for doc_index, document in enumerate(result.get("documents") or [], start=1):
            if not isinstance(document, dict) or not document.get("base64EncodedDocument"):
                continue
            name = _safe_name(str(document.get("name") or f"pod-{doc_index}.pdf"))
            key = await _store_blob(
                document["base64EncodedDocument"], f"{base}-{doc_index}-{name}", _content_type(name)
            )
            if key:
                document.pop("base64EncodedDocument")
                document["storageKey"] = key
    return stored


@router.post("/easytrans")
async def easytrans_webhook(
    request: Request,
    x_api_key: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
):
    expected = settings.easytrans_webhook_api_key
    if expected:
        if not x_api_key or not secrets.compare_digest(x_api_key, expected):
            logger.warning("EasyTrans webhook rejected: bad X-API-Key")
            return Response("Invalid API key", status_code=status.HTTP_401_UNAUTHORIZED)
    else:
        logger.warning(
            "EasyTrans webhook accepted WITHOUT verification — set EASYTRANS_WEBHOOK_API_KEY. "
            "Received X-API-Key: %s", x_api_key,
        )

    try:
        payload = await request.json()
    except Exception:
        logger.warning("EasyTrans webhook: body is not JSON: %r", (await request.body())[:500])
        return Response(status_code=status.HTTP_200_OK)
    if not isinstance(payload, dict):
        logger.warning("EasyTrans webhook: unexpected body shape: %r", payload)
        return Response(status_code=status.HTTP_200_OK)

    order = payload.get("order") or {}
    order_no = str(order["orderNo"]) if order.get("orderNo") is not None else None
    order_status = order.get("status")
    sub_status_name = order.get("subStatusName") or None
    sub_status_id = order.get("subStatusId")
    event_time = _parse_event_time(payload.get("eventTime")) or datetime.now(timezone.utc)

    logger.info(
        "EasyTrans webhook: order %s status=%s substatus=%s at %s",
        order_no, order_status, sub_status_name, event_time.isoformat(),
    )

    manifest = None
    if order_no:
        manifest = (await db.execute(
            select(HawbManifest).where(HawbManifest.mytransport_order_no == order_no)
        )).scalars().first()
    if manifest is None:
        logger.warning("EasyTrans webhook: no manifest has order no %s — logged only", order_no)

    event_id = uuid.uuid4()
    stored_payload = await _offload_pod(payload, order_no or "unknown", event_id)

    db.add(EasytransWebhookEvent(
        id=event_id,
        manifest_id=manifest.id if manifest else None,
        order_no=order_no,
        status=order_status,
        sub_status_id=sub_status_id if isinstance(sub_status_id, int) else None,
        sub_status_name=sub_status_name,
        event_time=event_time,
        payload=stored_payload,
    ))

    # Events can arrive out of order (a driver re-signing a reopened stop, or
    # a subcarrier batch) — only move the manifest's summary forward in time.
    if manifest is not None and (
        manifest.easytrans_status_at is None or event_time >= manifest.easytrans_status_at
    ):
        manifest.easytrans_status = order_status
        manifest.easytrans_sub_status = sub_status_name
        manifest.easytrans_status_at = event_time

    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("EasyTrans webhook: failed to save event for order %s", order_no)
        # A DB failure is the one case worth surfacing — the driver sees this
        # text and can tell the office the status didn't reach Horizon.
        return Response("Horizon could not save this status update", status_code=500)

    return Response(status_code=status.HTTP_200_OK)
