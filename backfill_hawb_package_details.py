"""
One-time backfill: split the combined temperature_range / dimensions strings
on hawb_jobs (e.g. "Uncontrolled / -80") back out into each package's own
temperature_range / dimensions field, for jobs ingested before packages
captured this per-row (see hawb_system_prompt.txt).

Only touches jobs with more than one package where none of the packages
already carry their own temperature_range/dimensions, and only when the
combined string splits into exactly as many parts as there are packages —
anything ambiguous is left alone.

Run from the project root:
    python backfill_hawb_package_details.py
"""
import asyncio

# Import all models so SQLAlchemy can resolve all relationships before querying
import app.models  # noqa: F401 — registers every model with the Base metadata

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.hawb import HawbJob


def _split(value: str | None, expected: int) -> list[str] | None:
    if not value:
        return None
    parts = [p.strip() for p in value.split(" / ")]
    if len(parts) != expected:
        return None
    return parts


async def backfill():
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(HawbJob))
        jobs = result.scalars().all()

    candidates = [
        j for j in jobs
        if len(j.packages) > 1
        and not any(p.get("temperature_range") or p.get("dimensions") for p in j.packages)
    ]
    print(f"Multi-package jobs missing per-package temp/dims: {len(candidates)}")
    if not candidates:
        print("Nothing to backfill.")
        return

    updated = 0
    skipped = 0
    for job in candidates:
        n = len(job.packages)
        temps = _split(job.temperature_range, n)
        dims = _split(job.dimensions, n)
        if temps is None and dims is None:
            print(f"  SKIP {job.hawb_number} — combined values don't split into {n} parts cleanly")
            skipped += 1
            continue

        new_packages = []
        for i, pkg in enumerate(job.packages):
            new_pkg = dict(pkg)
            if temps is not None:
                new_pkg["temperature_range"] = temps[i]
            if dims is not None:
                new_pkg["dimensions"] = dims[i]
            new_packages.append(new_pkg)

        async with AsyncSessionLocal() as db:
            row = await db.get(HawbJob, job.id)
            if row:
                row.packages = new_packages
                await db.commit()
        print(f"  OK   {job.hawb_number}  temp={temps}  dims={dims}")
        updated += 1

    print(f"\nDone. Updated: {updated} | Skipped: {skipped}")


if __name__ == "__main__":
    asyncio.run(backfill())
