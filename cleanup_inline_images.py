"""
Remove inline signature images that were incorrectly saved as attachments
by the Sync endpoint before the contentId filter was added.

Targets attachments where:
  - content_type starts with 'image/'
  - filename matches Outlook's auto-naming pattern: image<digits>.(png|jpg|jpeg|gif|webp)
"""
import asyncio
import re
from sqlalchemy import select, delete
from app.database import AsyncSessionLocal
import app.models  # noqa: F401 — register all models

from app.models.email_message import EmailAttachment

INLINE_PATTERN = re.compile(r'^image\d+\.(png|jpe?g|gif|webp)$', re.IGNORECASE)


async def run():
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(EmailAttachment).where(
                EmailAttachment.content_type.like("image/%")
            )
        )
        all_image_atts = result.scalars().all()

        to_delete = [a for a in all_image_atts if INLINE_PATTERN.match(a.filename)]
        print(f"Total image attachments: {len(all_image_atts)}")
        print(f"Inline images to remove: {len(to_delete)}")
        for a in to_delete:
            print(f"  DELETE: {a.filename} ({a.size_bytes} B) — {a.id}")

        if not to_delete:
            print("Nothing to delete.")
            return

        ids = [a.id for a in to_delete]
        await db.execute(
            delete(EmailAttachment).where(EmailAttachment.id.in_(ids))
        )
        await db.commit()
        print(f"\nDeleted {len(ids)} inline image attachment(s).")


if __name__ == "__main__":
    asyncio.run(run())
