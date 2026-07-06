"""
One-time backfill: populate graph_message_id on inbound EmailMessage rows
that were ingested before this column was added.

Run from the project root:
    python backfill_graph_message_ids.py
"""
import asyncio
import httpx
from sqlalchemy import select

# Import all models so SQLAlchemy can resolve all relationships before querying
import app.models  # noqa: F401 — registers every model with the Base metadata

from app.database import AsyncSessionLocal
from app.models.email_message import EmailMessage
from app.tasks.oauth2 import get_graph_token
from app.core.config import settings

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def _graph_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


async def backfill():
    mailbox = settings.MAILBOX_EMAIL
    if not mailbox:
        print("MAILBOX_EMAIL not configured — aborting")
        return

    token = get_graph_token(settings)

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(EmailMessage).where(
                EmailMessage.direction == "inbound",
                EmailMessage.graph_message_id.is_(None),
                EmailMessage.message_id.is_not(None),
            )
        )
        emails = result.scalars().all()

    print(f"Inbound emails missing graph_message_id: {len(emails)}")
    if not emails:
        print("Nothing to backfill.")
        return

    updated = 0
    not_found = 0

    with httpx.Client(timeout=30) as client:
        for em in emails:
            mid = em.message_id
            try:
                # Graph API filter: search ALL mail folders by internetMessageId
                resp = client.get(
                    f"{GRAPH_BASE}/users/{mailbox}/messages",
                    params={
                        "$filter": f"internetMessageId eq '{mid}'",
                        "$select": "id,internetMessageId",
                        "$top": "1",
                    },
                    headers=_graph_headers(token),
                )
                if resp.status_code != 200:
                    print(f"  Graph {resp.status_code} for {mid[:50]}… — skipping")
                    continue

                msgs = resp.json().get("value", [])
                if not msgs:
                    # Try Inbox specifically (some clients filter all-messages endpoint)
                    resp2 = client.get(
                        f"{GRAPH_BASE}/users/{mailbox}/mailFolders/Inbox/messages",
                        params={
                            "$filter": f"internetMessageId eq '{mid}'",
                            "$select": "id,internetMessageId",
                            "$top": "1",
                        },
                        headers=_graph_headers(token),
                    )
                    if resp2.status_code == 200:
                        msgs = resp2.json().get("value", [])

                if msgs:
                    graph_id = msgs[0]["id"]
                    async with AsyncSessionLocal() as db:
                        em_row = await db.get(EmailMessage, em.id)
                        if em_row:
                            em_row.graph_message_id = graph_id
                            await db.commit()
                    print(f"  OK  booking={em.booking_id} | {mid[:50]}…")
                    updated += 1
                else:
                    print(f"  NOT FOUND in Graph: {mid[:50]}…")
                    not_found += 1

            except Exception as exc:
                print(f"  ERROR for {mid[:50]}…: {exc}")

    print(f"\nDone. Updated: {updated} | Not found in Graph: {not_found}")


if __name__ == "__main__":
    asyncio.run(backfill())
