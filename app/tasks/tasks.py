import asyncio
import html as html_lib
import re
import uuid
from datetime import date, datetime, timezone
from email.header import decode_header
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
import email as email_lib

import httpx

from app.tasks.celery_app import celery_app
from app.core.config import settings
from app.tasks.oauth2 import get_graph_token

POINTER_KEY = "bts:allocation:pointer"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def _run(coro):
    return asyncio.run(coro)


def _extract_body(msg) -> tuple[str | None, str | None]:
    body_text = None
    body_html = None
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = part.get("Content-Disposition", "")
            if "attachment" in cd:
                continue
            charset = part.get_content_charset() or "utf-8"
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            if ct == "text/plain" and body_text is None:
                body_text = payload.decode(charset, errors="replace")
            elif ct == "text/html" and body_html is None:
                body_html = payload.decode(charset, errors="replace")
    else:
        ct = msg.get_content_type()
        charset = msg.get_content_charset() or "utf-8"
        payload = msg.get_payload(decode=True)
        if payload:
            text = payload.decode(charset, errors="replace")
            if ct == "text/html":
                body_html = text
            else:
                body_text = text
    return body_text, body_html


def _extract_attachments(msg) -> list[dict]:
    attachments = []
    for part in msg.walk():
        cd = part.get("Content-Disposition", "")
        if "attachment" not in cd:
            continue
        raw_filename = part.get_filename()
        if not raw_filename:
            continue
        decoded = decode_header(raw_filename)
        filename = "".join(
            p.decode(enc or "utf-8") if isinstance(p, bytes) else p
            for p, enc in decoded
        )
        data = part.get_payload(decode=True)
        if data:
            attachments.append({
                "filename": filename,
                "content_type": part.get_content_type() or "application/octet-stream",
                "data": data,
            })
    return attachments


def _parse_addresses(header_val: str | None) -> str:
    """Return a comma-separated string of all email addresses from a To/CC header."""
    if not header_val:
        return ""
    return ", ".join(addr for _, addr in getaddresses([header_val]) if addr)


def _strip_re_prefix(subject: str) -> str:
    """Strip leading Re:/Fwd: and [EXTERNAL]/[EXT]/[CAUTION] tags to get the base subject."""
    s = subject.strip()
    while True:
        lower = s.lower()
        if lower.startswith("re:"):
            s = s[3:].lstrip()
        elif lower.startswith("fwd:"):
            s = s[4:].lstrip()
        elif lower.startswith("fw:"):
            s = s[3:].lstrip()
        elif s.startswith("["):
            # Strip bracketed tags like [EXTERNAL], [EXT], [CAUTION], [EXTERNAL EMAIL]
            end = s.find("]")
            if end != -1:
                s = s[end + 1:].lstrip()
            else:
                break
        else:
            break
    return s


async def _find_existing_booking_id(db, in_reply_to: str, references: str, sender_email: str, subject: str, conversation_id: str | None = None) -> str | None:
    """
    Detect whether this email is a reply to an existing booking.

    Strategy (in priority order):
    1. In-Reply-To / References headers → explicit reply chain (highest precision)
    2. conversationId — only when subject has Re:/Fwd: prefix (confirms it's a reply,
       not a fresh booking request that shares an Outlook conversation thread)
    3. Subject-based fallback — strip Re:/Fwd:/[EXTERNAL] and match stored subject
    """
    from sqlalchemy import select
    from app.models.email_message import EmailMessage
    from app.models.booking import Booking

    # 1. Header-based threading (most precise — explicit reply chain)
    for header_val in [in_reply_to, references]:
        if not header_val:
            continue
        for mid in header_val.split():
            mid = mid.strip()
            if not mid:
                continue
            result = await db.execute(
                select(EmailMessage.booking_id)
                .where(EmailMessage.message_id == mid)
                .limit(1)
            )
            booking_id = result.scalar_one_or_none()
            if booking_id:
                return booking_id

    # 2. conversationId — only when subject has Re:/Fwd: prefix.
    # A plain (non-reply) email sharing a conversationId is a separate booking request.
    base_subject = _strip_re_prefix(subject)
    is_reply_or_forward = base_subject != subject.strip()
    if conversation_id and is_reply_or_forward:
        result = await db.execute(
            select(EmailMessage.booking_id)
            .where(EmailMessage.conversation_id == conversation_id)
            .limit(1)
        )
        booking_id = result.scalar_one_or_none()
        if booking_id:
            return booking_id

    # 3. Subject-based fallback — strip Re:/[EXTERNAL]/etc and match by subject only.
    # Do NOT filter by sender_email: replies can come from any participant in the thread.
    # Also try matching bookings that were stored with a Fw: prefix (forwarded emails).
    if is_reply_or_forward:
        from sqlalchemy import or_
        result = await db.execute(
            select(Booking.id)
            .where(
                or_(
                    Booking.subject == base_subject,
                    Booking.subject.ilike(f"fw: {base_subject}"),
                    Booking.subject.ilike(f"fwd: {base_subject}"),
                )
            )
            .order_by(Booking.received_at.desc())
            .limit(1)
        )
        booking_id = result.scalar_one_or_none()
        if booking_id:
            return booking_id

    return None


async def _save_attachments(db, raw_attachments: list[dict], booking_id: str, email_msg_id: uuid.UUID):
    from app.models.email_message import EmailAttachment
    from app.storage import s3_key, upload_bytes
    if not raw_attachments:
        return
    for att in raw_attachments:
        safe_name = Path(att["filename"]).name
        key = s3_key(booking_id, str(email_msg_id), safe_name)
        try:
            await upload_bytes(att["data"], key, att.get("content_type", "application/octet-stream"))
        except Exception as e:
            print(f"[BTS] S3 upload failed for {safe_name}: {e} — attachment skipped")
            continue
        db.add(EmailAttachment(
            message_id=email_msg_id,
            filename=safe_name,
            content_type=att["content_type"],
            size_bytes=len(att["data"]),
            storage_path=key,
        ))


# ------------------------------------------------------------------ #
#  Graph API helpers                                                   #
# ------------------------------------------------------------------ #

def _graph_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _graph_get_messages(client: httpx.Client, token: str, mailbox: str, allowed_sender: str, lookback_minutes: int = 1440) -> list[dict]:
    """Fetch inbound messages received in the last `lookback_minutes` across ALL folders.

    Queries the top-level /messages endpoint (not just Inbox) so emails that
    Outlook auto-moved to Deleted Items (e.g. "Ignored" conversations), Clutter,
    or other folders are still picked up. Filters to inbound-only by excluding
    messages sent from the mailbox itself — outbound messages are handled separately
    by the Sent Items poller.
    ProcessedEmail dedup prevents reprocessing.
    """
    from datetime import timedelta
    select_fields = (
        "id,subject,from,toRecipients,ccRecipients,body,"
        "receivedDateTime,internetMessageId,conversationId,"
        "categories,hasAttachments,internetMessageHeaders"
    )
    since = (datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Note: Graph does not support `ne` on nested complex-type properties.
    # Outbound messages are filtered out in the processing loop via a Python check.
    filter_clause = f"receivedDateTime ge {since}"
    if allowed_sender:
        filter_clause += f" and from/emailAddress/address eq '{allowed_sender}'"

    messages = []
    url = None
    while True:
        if url is None:
            resp = client.get(
                f"{GRAPH_BASE}/users/{mailbox}/messages",
                params={"$filter": filter_clause, "$select": select_fields, "$top": "50"},
                headers=_graph_headers(token),
            )
        else:
            resp = client.get(url, headers=_graph_headers(token))
        resp.raise_for_status()
        data = resp.json()
        messages.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
        if not url:
            break
    return messages


def _graph_get_attachments(client: httpx.Client, token: str, mailbox: str, msg_id: str) -> tuple[list[dict], dict[str, str]]:
    """
    Returns (regular_attachments, inline_cid_map).
    regular_attachments: non-inline files for S3 storage.
    inline_cid_map: contentId → base64 data URI, for replacing cid: src in HTML.
    """
    import base64
    import time
    url = f"{GRAPH_BASE}/users/{mailbox}/messages/{msg_id}/attachments"
    for attempt in range(3):
        resp = client.get(url, headers=_graph_headers(token))
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "10"))
            print(f"[BTS] Attachments rate-limited (429) — waiting {retry_after}s (attempt {attempt + 1}/3)")
            time.sleep(retry_after)
            continue
        resp.raise_for_status()
        break
    regular: list[dict] = []
    inline_map: dict[str, str] = {}
    for att in resp.json().get("value", []):
        if att.get("@odata.type") != "#microsoft.graph.fileAttachment":
            continue
        raw = att.get("contentBytes", "")
        content_type = att.get("contentType", "application/octet-stream")
        content_id = (att.get("contentId") or "").strip("<>").strip()
        is_image = content_type.lower().startswith("image/")
        # Only treat images with a contentId as inline — PDFs and other non-images go to S3
        if content_id and is_image:
            if raw:
                inline_map[content_id] = f"data:{content_type};base64,{raw}"
        else:
            regular.append({
                "filename": att.get("name", "attachment"),
                "content_type": content_type,
                "data": base64.b64decode(raw) if raw else b"",
            })
    return regular, inline_map


def _apply_inline_images(html: str, inline_map: dict[str, str]) -> str:
    """Replace all cid: references in HTML with base64 data URIs (case-insensitive)."""
    import re
    for content_id, data_uri in inline_map.items():
        pattern = re.compile(re.escape(f"cid:{content_id}"), re.IGNORECASE)
        before = len(pattern.findall(html))
        html = pattern.sub(data_uri, html)
        print(f"[BTS] CID replace '{content_id}': {before} occurrence(s)")
    return html


def _plain_text_from_html(html: str) -> str:
    """Rough HTML-to-text fallback for feeding an email body to the HAWB extractor
    when Graph returns an HTML-only body (no text/plain part)."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<(br|/p|/div|/tr)\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html_lib.unescape(text).strip()


def _graph_mark_read(client: httpx.Client, token: str, mailbox: str, msg_id: str):
    url = f"{GRAPH_BASE}/users/{mailbox}/messages/{msg_id}"
    client.patch(url, headers=_graph_headers(token), json={"isRead": True})


def _graph_get_sent_items(client: httpx.Client, token: str, mailbox: str, lookback_minutes: int = 60) -> list[dict]:
    from datetime import timedelta
    select_fields = (
        "id,subject,from,toRecipients,ccRecipients,body,"
        "sentDateTime,internetMessageId,conversationId,"
        "hasAttachments,internetMessageHeaders"
    )
    since = (datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    messages = []
    url = None
    while True:
        if url is None:
            resp = client.get(
                f"{GRAPH_BASE}/users/{mailbox}/mailFolders/SentItems/messages",
                params={"$filter": f"sentDateTime ge {since}", "$select": select_fields, "$top": "50"},
                headers=_graph_headers(token),
            )
        else:
            resp = client.get(url, headers=_graph_headers(token))
        resp.raise_for_status()
        data = resp.json()
        messages.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
        if not url:
            break
    return messages


def _parse_graph_addresses(recipients: list[dict]) -> str:
    return ", ".join(r["emailAddress"]["address"] for r in recipients if r.get("emailAddress", {}).get("address"))


def _get_internet_header(msg: dict, name: str) -> str:
    for h in msg.get("internetMessageHeaders") or []:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


# ------------------------------------------------------------------ #
#  Email polling                                                       #
# ------------------------------------------------------------------ #

@celery_app.task(name="app.tasks.tasks.poll_email_inbox", bind=True, max_retries=3)
def poll_email_inbox(self):
    try:
        _run(_poll_inbox_async())
    except Exception as exc:
        raise self.retry(exc=exc, countdown=10)


@celery_app.task(name="app.tasks.tasks.renew_graph_subscription", bind=True, max_retries=3)
def renew_graph_subscription(self):
    """Celery task to renew the Graph webhook subscription every 2 days."""
    async def _renew():
        from app.services.graph_subscription import renew_subscription
        await renew_subscription()
    try:
        _run(_renew())
    except Exception as exc:
        raise self.retry(exc=exc, countdown=60)


async def _poll_inbox_async():
    import redis.asyncio as aioredis
    from sqlalchemy import select
    from app.models.booking import Booking
    from app.models.agent import Agent
    from app.models.attendance import Attendance
    from app.models.email_message import EmailMessage
    from app.database import AsyncSessionLocal

    redis = aioredis.from_url(settings.REDIS_URL)

    try:
        token = get_graph_token(settings)
        mailbox = settings.MAILBOX_EMAIL
    except Exception as e:
        print(f"[BTS] Token acquisition failed — skipping poll: {e}")
        await redis.aclose()
        return

    # Parse cutoff datetime once
    cutoff_dt: datetime | None = None
    if settings.PROCESS_EMAILS_SINCE:
        try:
            cutoff_dt = datetime.fromisoformat(settings.PROCESS_EMAILS_SINCE)
            if cutoff_dt.tzinfo is None:
                cutoff_dt = cutoff_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            print(f"[BTS] Invalid PROCESS_EMAILS_SINCE format, ignoring: {settings.PROCESS_EMAILS_SINCE}")

    with httpx.Client(timeout=30) as client:
        # ── Phase 1: Inbound messages ─────────────────────────────────────────
        # Wrapped in try/except so a Graph API error here never prevents the
        # Sent Items poller (Phase 2) from running.
        try:
            messages = _graph_get_messages(client, token, mailbox, settings.ALLOWED_SENDER)
            print(f"[BTS] Messages from Graph API (last 24h): {len(messages)} | sender-filter: '{settings.ALLOWED_SENDER}' | cutoff: {cutoff_dt}")
        except Exception as e:
            print(f"[BTS] Inbox fetch error — skipping inbound phase: {e}")
            messages = []

        for msg in messages:
            graph_msg_id = msg["id"]

            # Parse received time
            received_str = msg.get("receivedDateTime", "")
            try:
                sent_at = datetime.fromisoformat(received_str.replace("Z", "+00:00")) if received_str else datetime.now(timezone.utc)
            except ValueError:
                sent_at = datetime.now(timezone.utc)

            # Skip emails before cutoff
            if cutoff_dt and sent_at < cutoff_dt:
                print(f"[BTS] Skipping email before cutoff ({sent_at})")
                continue

            subject = msg.get("subject") or ""
            sender_email = msg.get("from", {}).get("emailAddress", {}).get("address", "")

            # Skip outbound messages — Graph's /messages endpoint returns all folders
            # including Sent Items. Outbound messages are handled by Phase 2 below.
            if sender_email.lower() == mailbox.lower():
                continue
            raw_message_id = msg.get("internetMessageId")

            # Dedup check before any additional API calls — already-processed emails
            # must not trigger an attachment fetch (which causes Graph 429 rate limits).
            # Wrapped defensively: an error here (e.g. booking schema not present in this
            # DB) must not abort the whole poll — HAWB ingestion below is independent of
            # the booking pipeline and must still get a chance to run.
            if raw_message_id:
                try:
                    async with AsyncSessionLocal() as check_db:
                        from sqlalchemy import select as sa_select
                        from app.models.processed_email import ProcessedEmail
                        exists = await check_db.scalar(
                            sa_select(ProcessedEmail.message_id)
                            .where(ProcessedEmail.message_id == raw_message_id)
                            .limit(1)
                        )
                        if exists:
                            print(f"[BTS] Already processed {raw_message_id[:40]}… — skipping")
                            _graph_mark_read(client, token, mailbox, graph_msg_id)
                            continue
                except Exception as e:
                    print(f"[BTS] Booking dedup check failed (continuing anyway): {e}")

            to_emails = _parse_graph_addresses(msg.get("toRecipients") or [])
            cc_emails = _parse_graph_addresses(msg.get("ccRecipients") or []) or None

            # Outlook categories come as a native list — no parsing needed
            # Categories matching a known priority set the booking priority.
            # The first non-priority category is used as the agent name for direct assignment.
            PRIORITY_CATEGORIES = {"Very Urgent", "Urgent", "Not Urgent"}
            categories: list[str] = msg.get("categories") or []
            category_priority: str | None = None
            category_agent_name: str | None = None
            for cat in categories:
                if cat in PRIORITY_CATEGORIES:
                    category_priority = category_priority or cat
                else:
                    category_agent_name = category_agent_name or cat

            # Email body
            body_obj = msg.get("body") or {}
            body_type = body_obj.get("contentType", "text")
            body_content = body_obj.get("content", "")
            body_text = body_content if body_type == "text" else None
            body_html = body_content if body_type == "html" else None

            # Attachments (inline images embedded into HTML as data URIs; regular files go to S3)
            raw_attachments: list[dict] = []
            if msg.get("hasAttachments"):
                raw_attachments, inline_map = _graph_get_attachments(client, token, mailbox, graph_msg_id)
                if inline_map and body_html:
                    body_html = _apply_inline_images(body_html, inline_map)

            # HAWB pipeline — runs independently of booking creation below, filtered to
            # PDF attachments only, deduped via its own hawb_processed_emails table.
            pdf_attachments = [a for a in raw_attachments if (a.get("filename") or "").lower().endswith(".pdf")]
            if pdf_attachments and raw_message_id:
                try:
                    from app.services import hawb_ingest
                    hawb_body_text = body_text or (_plain_text_from_html(body_html) if body_html else None)
                    await hawb_ingest.process_email_attachments(
                        message_id=raw_message_id,
                        sender_email=sender_email,
                        subject=subject,
                        pdf_attachments=pdf_attachments,
                        body_text=hawb_body_text,
                    )
                except Exception as e:
                    print(f"[BTS] HAWB ingest error for {raw_message_id[:40]}…: {e}")

            # Mark the message read once we've attempted it — independent of whatever
            # the (separate, legacy) booking pipeline below does with it, so a HAWB
            # email doesn't stay unread just because booking creation errors out first.
            try:
                _graph_mark_read(client, token, mailbox, graph_msg_id)
            except Exception as e:
                print(f"[BTS] Mark-as-read failed for {graph_msg_id}: {e}")

            # Threading headers
            in_reply_to = _get_internet_header(msg, "In-Reply-To")
            references = _get_internet_header(msg, "References")
            conversation_id = msg.get("conversationId")

            print(f"[BTS] Processing email from: {sender_email} | Subject: {subject}")

            email_msg_id = uuid.uuid4()

            async with AsyncSessionLocal() as db:
                try:
                    existing_booking_id = await _find_existing_booking_id(
                        db, in_reply_to, references, sender_email, subject, conversation_id
                    )

                    if existing_booking_id:
                        from datetime import timezone as _tz
                        from sqlalchemy.orm import selectinload as _sil
                        bk_res = await db.execute(
                            select(Booking)
                            .options(_sil(Booking.agent), _sil(Booking.support_agents))
                            .where(Booking.id == existing_booking_id)
                        )
                        booking_obj = bk_res.scalar_one_or_none()
                        reopened = False
                        if booking_obj:
                            now_utc = datetime.now(_tz.utc)
                            booking_obj.updated_at = now_utc
                            booking_obj.last_email_at = now_utc

                        email_record = EmailMessage(
                            id=email_msg_id,
                            booking_id=existing_booking_id,
                            message_id=raw_message_id,
                            in_reply_to=in_reply_to or None,
                            conversation_id=conversation_id,
                            graph_message_id=graph_msg_id,
                            direction="inbound",
                            from_email=sender_email,
                            to_email=to_emails or mailbox,
                            cc_emails=cc_emails,
                            subject=subject,
                            body_text=body_text,
                            body_html=body_html,
                            sent_at=sent_at,
                        )
                        db.add(email_record)
                        await db.flush()
                        await _save_attachments(db, raw_attachments, existing_booking_id, email_msg_id)
                        if booking_obj:
                            from app.utils.notify import notify_roles, notify_user
                            reply_title = "New email reply"
                            reply_body = f"Booking {existing_booking_id} — {booking_obj.subject} received a new reply from {sender_email}"
                            await notify_roles(db, ['admin', 'supervisor'], reply_title, reply_body, "email_reply", existing_booking_id)
                            notified_uids = set()
                            if booking_obj.agent and booking_obj.agent.user_id:
                                await notify_user(db, booking_obj.agent.user_id, reply_title, reply_body, "email_reply", existing_booking_id)
                                notified_uids.add(booking_obj.agent.user_id)
                            for sa in booking_obj.support_agents:
                                if sa.user_id and sa.user_id not in notified_uids:
                                    await notify_user(db, sa.user_id, reply_title, reply_body, "email_reply", existing_booking_id)
                                    notified_uids.add(sa.user_id)
                        if raw_message_id:
                            from app.models.processed_email import ProcessedEmail
                            db.add(ProcessedEmail(message_id=raw_message_id))
                        await db.commit()
                        _graph_mark_read(client, token, mailbox, graph_msg_id)
                        print(f"[BTS] Reply appended to existing booking: {existing_booking_id} | Reopened: {reopened} | Attachments: {len(raw_attachments)}")
                        import json as _json
                        await redis.publish("bts:events", _json.dumps({"type": "new_message", "booking_id": existing_booking_id, "reopened": reopened}))

                    else:
                        if not msg.get("subject"):
                            print(f"[BTS] Skipping email with no subject from {sender_email} — no booking created")
                            _graph_mark_read(client, token, mailbox, graph_msg_id)
                            continue

                        # Priority: Outlook category takes precedence over subject keywords
                        if category_priority:
                            priority = category_priority
                        else:
                            priority = "Blank"

                        from sqlalchemy import text as _text
                        _seq = await db.execute(_text("SELECT nextval('lw_booking_seq')"))
                        booking_id = f"LW{_seq.scalar():07d}"

                        booking = Booking(
                            id=booking_id,
                            subject=subject,
                            priority=priority,
                            sender_email=sender_email,
                            status="Pending",
                        )
                        db.add(booking)
                        await db.flush()

                        from app.models.booking import BookingEvent
                        db.add(BookingEvent(
                            booking_id=booking_id,
                            event="created",
                            actor_name="System",
                            new_value=f"Priority: {priority}",
                        ))

                        email_record = EmailMessage(
                            id=email_msg_id,
                            booking_id=booking_id,
                            message_id=raw_message_id,
                            conversation_id=conversation_id,
                            graph_message_id=graph_msg_id,
                            direction="inbound",
                            from_email=sender_email,
                            to_email=to_emails or mailbox,
                            cc_emails=cc_emails,
                            subject=subject,
                            body_text=body_text,
                            body_html=body_html,
                            sent_at=sent_at,
                        )
                        db.add(email_record)
                        await db.flush()
                        await _save_attachments(db, raw_attachments, booking_id, email_msg_id)

                        # Agent assignment: Outlook category name → direct assign, else round-robin
                        from app.models.allocation import AllocationLog
                        today = date.today()
                        assigned = None
                        log_pointer_value = -1
                        log_pool_size = 0

                        if category_agent_name:
                            cat_result = await db.execute(
                                select(Agent)
                                .join(Attendance, (Attendance.agent_id == Agent.id) & (Attendance.date == today), isouter=True)
                                .where(
                                    Attendance.status == "Present",
                                    Agent.name.ilike(f"%{category_agent_name}%"),
                                )
                                .limit(1)
                            )
                            assigned = cat_result.scalars().first()
                            if assigned:
                                print(f"[BTS] Category-assigned to: {assigned.name} (category: {category_agent_name})")

                        if not assigned:
                            rr_result = await db.execute(
                                select(Agent)
                                .join(Attendance, (Attendance.agent_id == Agent.id) & (Attendance.date == today), isouter=True)
                                .where(Attendance.status == "Present")
                                .order_by(Agent.name)
                            )
                            agents = rr_result.scalars().all()
                            if agents:
                                pool_size = len(agents)
                                pointer = await redis.incr(POINTER_KEY)
                                pointer -= 1
                                await redis.set(POINTER_KEY, (pointer + 1) % pool_size)
                                assigned = agents[pointer % pool_size]
                                log_pointer_value = pointer % pool_size
                                log_pool_size = pool_size
                                print(f"[BTS] Round-robin allocated to: {assigned.name}")

                        if assigned:
                            booking.agent_id = assigned.id
                            booking.status = "In Progress"
                            booking.assigned_at = datetime.now(timezone.utc)
                            db.add(AllocationLog(
                                booking_id=booking_id,
                                agent_id=assigned.id,
                                pointer_value=log_pointer_value,
                                pool_size=log_pool_size,
                                allocated_at=datetime.now(timezone.utc),
                            ))
                            method = "Category assignment" if log_pointer_value == -1 else f"Round-robin (pool: {log_pool_size})"
                            db.add(BookingEvent(
                                booking_id=booking_id,
                                event="agent_assigned",
                                actor_name="System",
                                new_value=assigned.name,
                                old_value=method,
                            ))
                            db.add(BookingEvent(
                                booking_id=booking_id,
                                event="status_changed",
                                actor_name="System",
                                old_value="Pending",
                                new_value="In Progress",
                            ))
                        else:
                            print(f"[BTS] No present agents — booking stays Pending")
                            db.add(BookingEvent(
                                booking_id=booking_id,
                                event="no_agents_available",
                                actor_name="System",
                                new_value="Booking stays Open — no present agents",
                            ))

                        from app.utils.notify import notify_roles, notify_user
                        await notify_roles(db, ['admin', 'supervisor'],
                            "New booking received",
                            f"Booking {booking_id} — {subject} from {sender_email}",
                            "booking_created", booking_id)
                        if assigned and assigned.user_id:
                            await notify_user(db, assigned.user_id,
                                "New booking assigned to you",
                                f"Booking {booking_id} — {subject} has been assigned to you",
                                "booking_assigned", booking_id)

                        if raw_message_id:
                            from app.models.processed_email import ProcessedEmail
                            db.add(ProcessedEmail(message_id=raw_message_id))
                        await db.commit()
                        _graph_mark_read(client, token, mailbox, graph_msg_id)
                        print(f"[BTS] New booking: {booking_id} | Status: {booking.status} | Priority: {priority} | Attachments: {len(raw_attachments)}")
                        import json as _json
                        await redis.publish("bts:events", _json.dumps({"type": "new_booking", "id": booking_id}))
                        await redis.publish("bts:events", _json.dumps({"type": "notification"}))

                except Exception as e:
                    await db.rollback()
                    print(f"[BTS] Error processing email: {e}")

        # ── Sent Items: capture outbound replies into existing bookings ──
        try:
            sent_messages = _graph_get_sent_items(client, token, mailbox, lookback_minutes=1440)
            print(f"[BTS] Sent items (last 24 hr): {len(sent_messages)}")
        except Exception as e:
            print(f"[BTS] Could not fetch sent items: {e}")
            sent_messages = []

        for msg in sent_messages:
            graph_msg_id = msg["id"]

            sent_str = msg.get("sentDateTime", "")
            try:
                sent_at = datetime.fromisoformat(sent_str.replace("Z", "+00:00")) if sent_str else datetime.now(timezone.utc)
            except ValueError:
                sent_at = datetime.now(timezone.utc)

            if cutoff_dt and sent_at < cutoff_dt:
                continue

            raw_message_id = msg.get("internetMessageId")
            in_reply_to = _get_internet_header(msg, "In-Reply-To")
            references = _get_internet_header(msg, "References")
            conversation_id = msg.get("conversationId")
            x_bts_id = _get_internet_header(msg, "X-BTS-Message-ID")

            print(f"[BTS] Sent item found: subject='{msg.get('subject', '')[:50]}' | conv_id={'yes' if conversation_id else 'NO'} | in_reply_to={'yes' if in_reply_to else 'NO'} | x_bts={'yes' if x_bts_id else 'NO'}")

            # Skip messages sent via BTS (reply endpoint stamps X-BTS-Message-ID
            # and records it in ProcessedEmail to prevent duplicates here)
            if x_bts_id:
                async with AsyncSessionLocal() as check_db:
                    from sqlalchemy import select as sa_select
                    from app.models.processed_email import ProcessedEmail
                    exists = await check_db.scalar(
                        sa_select(ProcessedEmail.message_id)
                        .where(ProcessedEmail.message_id == x_bts_id)
                        .limit(1)
                    )
                    if exists:
                        print(f"[BTS] Sent item skipped — BTS reply already recorded")
                        continue

            # Only process if we have some way to match to a booking
            if not in_reply_to and not references and not conversation_id:
                print(f"[BTS] Sent item skipped — no threading info at all")
                continue

            # Dedup check (read-only) — actual ProcessedEmail insert happens inside
            # the main transaction below so we don't permanently lose emails that
            # fail booking-matching on first attempt.
            if raw_message_id:
                async with AsyncSessionLocal() as check_db:
                    from sqlalchemy import select as sa_select
                    from app.models.processed_email import ProcessedEmail
                    exists = await check_db.scalar(
                        sa_select(ProcessedEmail.message_id)
                        .where(ProcessedEmail.message_id == raw_message_id)
                        .limit(1)
                    )
                    if exists:
                        continue

            subject = msg.get("subject") or "(No Subject)"
            sender_email = msg.get("from", {}).get("emailAddress", {}).get("address", "")
            to_emails = _parse_graph_addresses(msg.get("toRecipients") or [])
            cc_emails = _parse_graph_addresses(msg.get("ccRecipients") or []) or None

            body_obj = msg.get("body") or {}
            body_type = body_obj.get("contentType", "text")
            body_content = body_obj.get("content", "")
            body_text = body_content if body_type == "text" else None
            body_html = body_content if body_type == "html" else None

            # Fetch attachments before creating the EmailMessage so inline images
            # can be embedded into body_html before the record is persisted
            raw_attachments: list[dict] = []
            if msg.get("hasAttachments"):
                raw_attachments, inline_map = _graph_get_attachments(client, token, mailbox, graph_msg_id)
                if inline_map and body_html:
                    body_html = _apply_inline_images(body_html, inline_map)

            email_msg_id = uuid.uuid4()

            async with AsyncSessionLocal() as db:
                try:
                    existing_booking_id = await _find_existing_booking_id(
                        db, in_reply_to, references, sender_email, subject, conversation_id
                    )
                    if not existing_booking_id:
                        print(f"[BTS] Sent item skipped — no booking match | subject: {subject} | conv_id: {conversation_id} | in_reply_to: {in_reply_to[:60] if in_reply_to else None}")
                        continue

                    email_record = EmailMessage(
                        id=email_msg_id,
                        booking_id=existing_booking_id,
                        message_id=raw_message_id,
                        in_reply_to=in_reply_to or None,
                        conversation_id=conversation_id,
                        direction="outbound",
                        from_email=sender_email,
                        to_email=to_emails,
                        cc_emails=cc_emails,
                        subject=subject,
                        body_text=body_text,
                        body_html=body_html,
                        sent_at=sent_at,
                    )
                    db.add(email_record)
                    await db.flush()

                    await _save_attachments(db, raw_attachments, existing_booking_id, email_msg_id)

                    # Mark as processed in the same transaction so the email is never
                    # permanently lost if booking match succeeds but commit fails.
                    if raw_message_id:
                        from app.models.processed_email import ProcessedEmail
                        db.add(ProcessedEmail(message_id=raw_message_id))

                    await db.commit()
                    print(f"[BTS] Outbound reply saved → booking {existing_booking_id} | {subject}")
                    import json as _json
                    await redis.publish("bts:events", _json.dumps({"type": "new_message", "booking_id": existing_booking_id, "reopened": False}))

                except Exception as e:
                    await db.rollback()
                    print(f"[BTS] Error processing sent item: {e}")

    await redis.aclose()
