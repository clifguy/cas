"""Hold a real ``migrate_vault`` open on a vault's services.

One backfill of the migration is replaced by a stand-in that waits on a gate,
so the migration takes and keeps the vault's exclusion of pipeline work until
the test releases it.
"""

import asyncio
from datetime import datetime, timezone


class HeldBackfill:
    """Stands in for one migration backfill and holds the migration open."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()

    async def __call__(self) -> int:
        self.entered.set()
        await self.gate.wait()
        return 0


def hold_backfill_on(services) -> HeldBackfill:
    """Install a held backfill on ``services``'s maintenance service."""
    backfill = HeldBackfill()
    services.maintenance_service._divide_passages_over_input_bound = backfill
    return backfill


async def hold_migration(services) -> tuple[asyncio.Task, HeldBackfill, datetime]:
    """Run a real ``migrate_vault`` on ``services`` and hold it mid-run."""
    backfill = hold_backfill_on(services)
    started = datetime.now(timezone.utc)
    task = asyncio.create_task(services.maintenance_service.migrate_vault())
    await asyncio.wait_for(backfill.entered.wait(), timeout=5.0)
    return task, backfill, started


async def release_migration(task: asyncio.Task, backfill: HeldBackfill):
    """Let a held migration finish and return its report."""
    backfill.gate.set()
    return await asyncio.wait_for(task, timeout=5.0)
