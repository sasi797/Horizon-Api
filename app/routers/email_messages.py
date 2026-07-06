import asyncio
import base64
import html as _html
import uuid
import email.utils as email_utils
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.core.config import settings as core_settings
from app.dependencies import get_current_user, get_db
from app.models.booking import Booking
from app.models.email_message import EmailAttachment, EmailMessage
from app.schemas.email_message import EmailMessageOut
from app.tasks.oauth2 import get_graph_token

router = APIRouter(tags=["email-messages"])

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Graph inline fileAttachment limit; larger files need an upload session.
_LARGE_ATTACH_THRESHOLD = 3 * 1024 * 1024  # 3 MB
# Chunk size must be a multiple of 320 KB per Graph requirements.
_UPLOAD_CHUNK = 4 * 320 * 1024  # ~1.25 MB
# Maximum attachment size we accept from the client.
_MAX_ATTACH_SIZE = 20 * 1024 * 1024  # 20 MB


def _graph_error_msg(resp) -> str:
    """Return a concise, human-readable message from a Graph API error response."""
    try:
        err = resp.json().get("error", {})
        code = err.get("code", "")
        message = err.get("message", "")
        if "SizeExceeded" in code or "MessageSize" in code or resp.status_code == 413:
            return "File too large for this mailbox. Ask your admin to raise the attachment size limit."
        if code == "AccessDenied" or resp.status_code == 403:
            return "Permission denied — the app needs Mail.ReadWrite access in Azure AD."
        if message:
            return message[:200]
    except Exception:
        pass
    return f"Graph API error {resp.status_code}: {resp.text[:150]}"


def _is_throttled(resp) -> bool:
    """Return True if Graph is telling us to back off (rate limit or mailbox concurrency)."""
    if resp.status_code in (429, 503):
        return True
    try:
        code = resp.json().get("error", {}).get("code", "")
        return "Concurrency" in code or "RequestLimit" in code or "Throttle" in code
    except Exception:
        return False


async def _graph_send_with_retry(client: httpx.AsyncClient, method: str, url: str, **kwargs) -> httpx.Response:
    """Call a Graph send endpoint with up to 3 attempts on throttle errors."""
    for attempt in range(3):
        resp = await getattr(client, method)(url, **kwargs)
        if not _is_throttled(resp):
            return resp
        wait = int(resp.headers.get("Retry-After", "3"))
        if attempt < 2:
            await asyncio.sleep(wait)
    return resp


def _to_html(text: str) -> str:
    """Convert plain text (with \n line breaks) to minimal HTML for email clients."""
    return _html.escape(text).replace("\n", "<br>").replace("\r", "")


def _wrap_html(fragment: str) -> str:
    """Wrap a raw contentEditable innerHTML fragment in a minimal email-safe HTML document.

    Raw browser innerHTML uses <div>/<b>/<i>/<u> tags but lacks a font declaration.
    Wrapping ensures Outlook and other clients render the correct font and honour
    inline formatting tags rather than stripping them.
    """
    return (
        '<html><body style="font-family:Arial,sans-serif;font-size:14px;color:#000;">'
        f"{fragment}"
        "</body></html>"
    )


def _inline_attachment(att: dict) -> dict:
    return {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": att["filename"],
        "contentType": att["content_type"],
        "contentBytes": base64.b64encode(att["data"]).decode(),
    }


async def _attach_small(token: str, mailbox: str, draft_id: str, att: dict) -> None:
    """POST a single small attachment (≤3 MB) to a draft message."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{GRAPH_BASE}/users/{mailbox}/messages/{draft_id}/attachments",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=_inline_attachment(att),
        )
    if resp.status_code != 201:
        raise HTTPException(502, f"Attach failed: {_graph_error_msg(resp)}")


async def _upload_large_attachment(token: str, mailbox: str, draft_id: str, att: dict) -> None:
    """Upload a file >3 MB to a draft message via Graph upload session (chunked)."""
    data: bytes = att["data"]
    total = len(data)

    async with httpx.AsyncClient(timeout=60) as client:
        session_resp = await client.post(
            f"{GRAPH_BASE}/users/{mailbox}/messages/{draft_id}/attachments/createUploadSession",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"AttachmentItem": {
                "attachmentType": "file",
                "name": att["filename"],
                "size": total,
                "contentType": att["content_type"],
            }},
        )
    if session_resp.status_code != 201:
        raise HTTPException(502, f"Upload session failed: {_graph_error_msg(session_resp)}")

    upload_url = session_resp.json()["uploadUrl"]

    # The upload URL is pre-authenticated; no Authorization header needed for chunks.
    async with httpx.AsyncClient(timeout=300) as client:
        offset = 0
        while offset < total:
            end = min(offset + _UPLOAD_CHUNK, total)
            chunk = data[offset:end]
            put = await client.put(
                upload_url,
                headers={
                    "Content-Range": f"bytes {offset}-{end - 1}/{total}",
                    "Content-Length": str(len(chunk)),
                },
                content=chunk,
            )
            if put.status_code not in (200, 201, 202):
                raise HTTPException(502, f"Chunk upload failed at byte {offset}: {_graph_error_msg(put)}")
            offset = end


async def _reply_via_graph(
    mailbox: str,
    graph_message_id: str,
    recipients: list[str],
    body_text: str,
    attachments: list[dict],
    cc_recipients: list[str] | None = None,
    bts_message_id: str | None = None,
    body_html: str | None = None,
):
    """Send a threaded reply using Graph's /reply endpoint.

    This preserves In-Reply-To and References headers automatically, keeping
    the email chain intact in the recipient's mail client.
    For attachments >3 MB a draft-based flow is used with upload sessions.
    """
    token = get_graph_token(core_settings)

    small = [a for a in attachments if len(a["data"]) <= _LARGE_ATTACH_THRESHOLD]
    large = [a for a in attachments if len(a["data"]) > _LARGE_ATTACH_THRESHOLD]

    msg: dict = {
        "toRecipients": [{"emailAddress": {"address": r}} for r in recipients],
        "body": {"contentType": "HTML", "content": _wrap_html(body_html) if body_html else _to_html(body_text)},
    }
    if cc_recipients:
        msg["ccRecipients"] = [{"emailAddress": {"address": r}} for r in cc_recipients]
    if bts_message_id:
        msg["internetMessageHeaders"] = [{"name": "X-BTS-Message-ID", "value": bts_message_id}]

    if not large:
        # Fast path: single /reply call with inline attachments.
        if small:
            msg["attachments"] = [_inline_attachment(a) for a in small]
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await _graph_send_with_retry(
                client, "post",
                f"{GRAPH_BASE}/users/{mailbox}/messages/{graph_message_id}/reply",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"message": msg},
            )
        if resp.status_code != 202:
            raise HTTPException(502, f"Send failed: {_graph_error_msg(resp)}")
        return

    # Draft path for large attachments.
    # 1. Try createReply for proper In-Reply-To/References threading.
    #    Falls back to a standalone draft if the message type doesn't support createReply.
    # 1. createReply with msg inline — no separate PATCH needed.
    #    Falls back to standalone draft if the message type doesn't support createReply.
    draft_id: str | None = None
    async with httpx.AsyncClient(timeout=30) as client:
        cr_resp = await client.post(
            f"{GRAPH_BASE}/users/{mailbox}/messages/{graph_message_id}/createReply",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"message": msg},
        )
    if cr_resp.status_code == 201:
        draft_id = cr_resp.json()["id"]
    else:
        # Fallback: standalone draft (no threading headers — Graph blocks In-Reply-To on new messages).
        async with httpx.AsyncClient(timeout=30) as client:
            draft_resp = await client.post(
                f"{GRAPH_BASE}/users/{mailbox}/messages",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=msg,
            )
        if draft_resp.status_code != 201:
            raise HTTPException(502, f"Create draft failed: {_graph_error_msg(draft_resp)}")
        draft_id = draft_resp.json()["id"]

    # 2. Add small attachments inline, large ones via upload sessions.
    for att in small:
        await _attach_small(token, mailbox, draft_id, att)
    for att in large:
        await _upload_large_attachment(token, mailbox, draft_id, att)

    # 3. Send the draft.
    async with httpx.AsyncClient(timeout=30) as client:
        send_resp = await client.post(
            f"{GRAPH_BASE}/users/{mailbox}/messages/{draft_id}/send",
            headers={"Authorization": f"Bearer {token}"},
        )
    if send_resp.status_code != 202:
        raise HTTPException(502, f"Send draft failed: {_graph_error_msg(send_resp)}")


async def _send_via_graph(
    sender: str,
    recipients: list[str],
    subject: str,
    body_text: str,
    attachments: list[dict],
    cc_recipients: list[str] | None = None,
    message_id: str | None = None,
    body_html: str | None = None,
):
    """Fallback: send a new (non-threaded) email via sendMail.

    Only used when no graph_message_id is available for an inbound message
    (e.g. bookings created before graph_message_id was introduced).
    For attachments >3 MB a draft-based flow is used with upload sessions.
    """
    token = get_graph_token(core_settings)

    small = [a for a in attachments if len(a["data"]) <= _LARGE_ATTACH_THRESHOLD]
    large = [a for a in attachments if len(a["data"]) > _LARGE_ATTACH_THRESHOLD]

    msg: dict = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": _wrap_html(body_html) if body_html else _to_html(body_text)},
        "toRecipients": [{"emailAddress": {"address": r}} for r in recipients],
    }
    if cc_recipients:
        msg["ccRecipients"] = [{"emailAddress": {"address": r}} for r in cc_recipients]
    if message_id:
        msg["internetMessageHeaders"] = [{"name": "X-BTS-Message-ID", "value": message_id}]

    if not large:
        # Fast path: sendMail with inline attachments.
        if small:
            msg["attachments"] = [_inline_attachment(a) for a in small]
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await _graph_send_with_retry(
                client, "post",
                f"{GRAPH_BASE}/users/{sender}/sendMail",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"message": msg, "saveToSentItems": True},
            )
        if resp.status_code != 202:
            raise HTTPException(502, f"Send failed: {_graph_error_msg(resp)}")
        return

    # Draft path for large attachments.
    # 1. Create the draft.
    async with httpx.AsyncClient(timeout=30) as client:
        draft_resp = await client.post(
            f"{GRAPH_BASE}/users/{sender}/messages",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=msg,
        )
    if draft_resp.status_code != 201:
        raise HTTPException(502, f"Create draft failed: {_graph_error_msg(draft_resp)}")
    draft_id = draft_resp.json()["id"]

    # 2. Add small attachments inline, large ones via upload sessions.
    for att in small:
        await _attach_small(token, sender, draft_id, att)
    for att in large:
        await _upload_large_attachment(token, sender, draft_id, att)

    # 3. Send the draft.
    async with httpx.AsyncClient(timeout=30) as client:
        send_resp = await client.post(
            f"{GRAPH_BASE}/users/{sender}/messages/{draft_id}/send",
            headers={"Authorization": f"Bearer {token}"},
        )
    if send_resp.status_code != 202:
        raise HTTPException(502, f"Send draft failed: {_graph_error_msg(send_resp)}")


@router.get("/bookings/{booking_id}/messages", response_model=list[EmailMessageOut])
async def list_messages(
    booking_id: str,
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_user),
):
    result = await db.execute(
        select(EmailMessage)
        .where(EmailMessage.booking_id == booking_id)
        .options(selectinload(EmailMessage.attachments))
        .order_by(EmailMessage.sent_at.desc())
    )
    return result.scalars().all()


@router.post("/bookings/{booking_id}/reply", response_model=EmailMessageOut, status_code=201)
async def reply_to_booking(
    booking_id: str,
    body_text: str = Form(...),
    body_html: str | None = Form(None),
    files: list[UploadFile] = File(default=[]),
    to_emails: str | None = Form(None),
    cc_emails: str | None = Form(None),
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_user),
):
    booking = await db.get(Booking, booking_id)
    if not booking:
        raise HTTPException(404, "Booking not found")

    sender_addr = core_settings.MAILBOX_EMAIL
    if not sender_addr:
        raise HTTPException(503, "Email sending not configured (MAILBOX_EMAIL not set)")

    # Find the most recent inbound message that has a graph_message_id (for proper threading).
    # Fall back to any inbound message for recipient/subject info.
    result = await db.execute(
        select(EmailMessage)
        .where(
            EmailMessage.booking_id == booking_id,
            EmailMessage.direction == "inbound",
            EmailMessage.graph_message_id.is_not(None),
        )
        .order_by(EmailMessage.sent_at.desc())
        .limit(1)
    )
    thread_anchor = result.scalar_one_or_none()

    if thread_anchor is None:
        # Fallback: any inbound message (pre-migration records without graph_message_id)
        fallback_result = await db.execute(
            select(EmailMessage)
            .where(EmailMessage.booking_id == booking_id, EmailMessage.direction == "inbound")
            .order_by(EmailMessage.sent_at.asc())
            .limit(1)
        )
        thread_anchor = fallback_result.scalar_one_or_none()

    reply_subject = booking.subject
    if not reply_subject.lower().startswith("re:"):
        reply_subject = f"Re: {reply_subject}"

    # Use explicit recipients if provided (Reply vs Reply All vs Forward distinction)
    if to_emails:
        all_recipients = [a.strip() for a in to_emails.split(',') if a.strip() and a.strip() != sender_addr]
    else:
        all_recipients = []
        if thread_anchor:
            for addr_source in [thread_anchor.from_email, thread_anchor.to_email, thread_anchor.cc_emails]:
                if not addr_source:
                    continue
                for addr in addr_source.split(','):
                    addr = addr.strip()
                    if addr and addr != sender_addr and addr not in all_recipients:
                        all_recipients.append(addr)
    if not all_recipients:
        all_recipients.append(booking.sender_email)

    # Parse CC recipients (exclude the mailbox itself)
    cc_list = [a.strip() for a in cc_emails.split(',') if a.strip() and a.strip() != sender_addr] if cc_emails else []

    # Generate a Message-ID so future inbound replies can be threaded back
    from pathlib import Path
    from app.storage import s3_key, upload_bytes

    msg_uuid = uuid.uuid4()
    outbound_mid = email_utils.make_msgid(idstring=str(msg_uuid).replace("-", ""), domain=sender_addr.split("@")[-1])

    # Read uploaded files into memory for Graph API (base64) and S3 storage
    saved_files: list[dict] = []
    graph_attachments: list[dict] = []

    for upload in files:
        if not upload.filename:
            continue
        data = await upload.read()
        if len(data) > _MAX_ATTACH_SIZE:
            raise HTTPException(413, f"File '{Path(upload.filename).name}' exceeds the 20 MB attachment limit.")
        safe_name = Path(upload.filename).name
        content_type = upload.content_type or "application/octet-stream"
        key = s3_key(booking_id, str(msg_uuid), safe_name)
        await upload_bytes(data, key, content_type)
        saved_files.append({"filename": safe_name, "content_type": content_type, "size_bytes": len(data), "storage_path": key})
        graph_attachments.append({"filename": safe_name, "content_type": content_type, "data": data})

    # Send via Graph reply endpoint if we have a graph_message_id (preserves thread).
    # Fall back to sendMail for old bookings ingested before graph_message_id was added.
    if thread_anchor and thread_anchor.graph_message_id:
        await _reply_via_graph(
            mailbox=sender_addr,
            graph_message_id=thread_anchor.graph_message_id,
            recipients=all_recipients,
            body_text=body_text,
            attachments=graph_attachments,
            cc_recipients=cc_list if cc_list else None,
            bts_message_id=outbound_mid,
            body_html=body_html,
        )
    else:
        await _send_via_graph(
            sender=sender_addr,
            recipients=all_recipients,
            subject=reply_subject,
            body_text=body_text,
            attachments=graph_attachments,
            cc_recipients=cc_list if cc_list else None,
            message_id=outbound_mid,
            body_html=body_html,
        )

    # Persist outbound message record
    email_msg = EmailMessage(
        id=msg_uuid,
        booking_id=booking_id,
        message_id=outbound_mid,
        conversation_id=thread_anchor.conversation_id if thread_anchor else None,
        direction="outbound",
        from_email=sender_addr,
        to_email=", ".join(all_recipients),
        cc_emails=", ".join(cc_list) if cc_list else None,
        subject=reply_subject,
        body_text=body_text,
        body_html=body_html,
        in_reply_to=thread_anchor.message_id if thread_anchor else None,
    )
    db.add(email_msg)
    await db.flush()

    for sf in saved_files:
        db.add(EmailAttachment(
            message_id=email_msg.id,
            filename=sf["filename"],
            content_type=sf["content_type"],
            size_bytes=sf["size_bytes"],
            storage_path=sf["storage_path"],
        ))

    # Mark this message as processed so the Sent Items poller skips it
    from app.models.processed_email import ProcessedEmail
    db.add(ProcessedEmail(message_id=outbound_mid))
    await db.commit()
    await db.refresh(email_msg)
    result2 = await db.execute(
        select(EmailMessage)
        .where(EmailMessage.id == email_msg.id)
        .options(selectinload(EmailMessage.attachments))
    )
    return result2.scalar_one()


@router.post("/bookings/{booking_id}/sync-emails")
async def sync_booking_emails(
    booking_id: str,
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_user),
):
    """Fetch the full conversation history from Graph API for a booking and
    add any emails that are missing from the local database."""
    from datetime import timezone
    from app.models.processed_email import ProcessedEmail
    from app.storage import s3_key, upload_bytes

    booking = await db.get(Booking, booking_id)
    if not booking:
        raise HTTPException(404, "Booking not found")

    mailbox = core_settings.MAILBOX_EMAIL
    if not mailbox:
        raise HTTPException(503, "MAILBOX_EMAIL not configured")

    # Resolve the Outlook conversationId from any existing message for this booking
    conv_result = await db.execute(
        select(EmailMessage.conversation_id)
        .where(EmailMessage.booking_id == booking_id, EmailMessage.conversation_id.is_not(None))
        .limit(1)
    )
    conversation_id = conv_result.scalar_one_or_none()
    if not conversation_id:
        return {"synced": 0}

    token = get_graph_token(core_settings)
    auth_hdr = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    SELECT = (
        "id,subject,from,toRecipients,ccRecipients,body,"
        "receivedDateTime,sentDateTime,internetMessageId,conversationId,"
        "hasAttachments,internetMessageHeaders"
    )

    import asyncio as _asyncio

    all_msgs: list[dict] = []
    async with httpx.AsyncClient(timeout=30) as client:

        async def _graph_get(req_url: str | None) -> dict:
            """One Graph request with a single 429 retry honouring Retry-After."""
            for attempt in range(2):
                if req_url is None:
                    r = await client.get(
                        f"{GRAPH_BASE}/users/{mailbox}/messages",
                        params={
                            "$filter": f"conversationId eq '{conversation_id}'",
                            "$select": SELECT,
                            "$top": "50",
                        },
                        headers=auth_hdr,
                    )
                else:
                    r = await client.get(req_url, headers=auth_hdr)

                if r.status_code == 429:
                    wait = int(r.headers.get("Retry-After", "10"))
                    if attempt == 0:
                        await _asyncio.sleep(wait)
                        continue
                    raise HTTPException(429, "Microsoft Graph is rate-limited — please try again in a few seconds")

                if r.status_code != 200:
                    raise HTTPException(502, f"Graph error {r.status_code}: {r.text[:200]}")

                return r.json()
            raise HTTPException(502, "Graph request failed after retry")

        url: str | None = None
        while True:
            data = await _graph_get(url)
            all_msgs.extend(data.get("value", []))
            url = data.get("@odata.nextLink")
            if not url:
                break

        synced = 0
        for msg in all_msgs:
            raw_mid = msg.get("internetMessageId")
            graph_id = msg["id"]

            hdrs = msg.get("internetMessageHeaders") or []
            def _hdr(name: str) -> str:
                for h in hdrs:
                    if h.get("name", "").lower() == name.lower():
                        return h.get("value", "")
                return ""

            # Skip messages already saved — check both the real internetMessageId
            # and the BTS-internal ID stamped on app-sent replies (X-BTS-Message-ID).
            # This prevents duplicating BTS replies whose Graph ID differs from the
            # stored fake outbound_mid.
            if raw_mid:
                already_saved = await db.scalar(
                    select(EmailMessage.id)
                    .where(EmailMessage.message_id == raw_mid)
                    .limit(1)
                )
                if already_saved:
                    continue

            x_bts_id = _hdr("X-BTS-Message-ID")
            if x_bts_id:
                bts_already_saved = await db.scalar(
                    select(EmailMessage.id)
                    .where(EmailMessage.message_id == x_bts_id)
                    .limit(1)
                )
                if bts_already_saved:
                    continue

            from_email = msg.get("from", {}).get("emailAddress", {}).get("address", "")
            direction = "outbound" if from_email.lower() == mailbox.lower() else "inbound"

            time_str = msg.get("receivedDateTime") or msg.get("sentDateTime") or ""
            try:
                sent_at = datetime.fromisoformat(time_str.replace("Z", "+00:00")) if time_str else datetime.now(timezone.utc)
            except ValueError:
                sent_at = datetime.now(timezone.utc)

            def _addrs(lst: list | None) -> str:
                return ", ".join(
                    r["emailAddress"]["address"]
                    for r in (lst or [])
                    if r.get("emailAddress", {}).get("address")
                )

            body_obj = msg.get("body") or {}
            body_type = body_obj.get("contentType", "text").lower()
            body_content = body_obj.get("content", "")

            email_msg_id = uuid.uuid4()
            record = EmailMessage(
                id=email_msg_id,
                booking_id=booking_id,
                message_id=raw_mid,
                in_reply_to=_hdr("In-Reply-To") or None,
                conversation_id=msg.get("conversationId"),
                graph_message_id=graph_id,
                direction=direction,
                from_email=from_email,
                to_email=_addrs(msg.get("toRecipients")) or mailbox,
                cc_emails=_addrs(msg.get("ccRecipients")) or None,
                subject=msg.get("subject"),
                body_text=body_content if body_type == "text" else None,
                body_html=body_content if body_type == "html" else None,
                sent_at=sent_at,
            )
            db.add(record)
            await db.flush()

            if msg.get("hasAttachments"):
                att_resp = await client.get(
                    f"{GRAPH_BASE}/users/{mailbox}/messages/{graph_id}/attachments",
                    headers=auth_hdr,
                )
                if att_resp.status_code == 200:
                    import re as _re
                    inline_map: dict[str, str] = {}
                    for att in att_resp.json().get("value", []):
                        if att.get("@odata.type") != "#microsoft.graph.fileAttachment":
                            continue
                        raw_bytes = att.get("contentBytes", "")
                        data_bytes = base64.b64decode(raw_bytes) if raw_bytes else b""
                        filename = att.get("name", "attachment")
                        ct = att.get("contentType", "application/octet-stream")
                        content_id = (att.get("contentId") or "").strip("<>").strip()
                        is_image = ct.lower().startswith("image/")
                        # Inline CID images go into the HTML as data URIs (not S3)
                        if content_id and is_image and raw_bytes:
                            inline_map[content_id] = f"data:{ct};base64,{raw_bytes}"
                        else:
                            key = s3_key(booking_id, str(email_msg_id), filename)
                            await upload_bytes(data_bytes, key, ct)
                            db.add(EmailAttachment(
                                message_id=email_msg_id,
                                filename=filename,
                                content_type=ct,
                                size_bytes=len(data_bytes),
                                storage_path=key,
                            ))
                    # Replace cid: references in the stored HTML with base64 data URIs
                    if inline_map and record.body_html:
                        for cid, data_uri in inline_map.items():
                            pattern = _re.compile(_re.escape(f"cid:{cid}"), _re.IGNORECASE)
                            record.body_html = pattern.sub(data_uri, record.body_html)

            if raw_mid:
                from sqlalchemy.dialects.postgresql import insert as pg_insert
                from app.models.processed_email import ProcessedEmail
                await db.execute(
                    pg_insert(ProcessedEmail)
                    .values(message_id=raw_mid)
                    .on_conflict_do_nothing()
                )

            synced += 1

    await db.commit()
    return {"synced": synced}


@router.get("/email-attachments/{attachment_id}")
async def download_attachment(
    attachment_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_user),
):
    att = await db.get(EmailAttachment, attachment_id)
    if not att:
        raise HTTPException(404, "Attachment not found")
    from app.storage import presigned_url
    url = await presigned_url(att.storage_path)
    return {"url": url, "filename": att.filename}
