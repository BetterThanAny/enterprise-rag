from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import and_, or_, select

from enterprise_rag_core.config import Settings
from enterprise_rag_core.database import create_database_resources
from enterprise_rag_core.dispatching import JobDispatcher
from enterprise_rag_core.models import IndexJob, IndexJobStatus
from enterprise_rag_worker.dispatcher import DramatiqDispatcher


async def enqueue_recoverable_jobs(settings: Settings, dispatcher: JobDispatcher) -> int:
    engine, session_factory = create_database_resources(settings.database_url)
    now = datetime.now(UTC)
    async with session_factory() as session, session.begin():
        jobs = list(
            (
                await session.execute(
                    select(IndexJob).where(
                        or_(
                            IndexJob.status == IndexJobStatus.PENDING,
                            and_(
                                IndexJob.status == IndexJobStatus.RUNNING,
                                or_(
                                    IndexJob.lease_until.is_(None),
                                    IndexJob.lease_until <= now,
                                ),
                            ),
                        )
                    )
                )
            ).scalars()
        )
        for job in jobs:
            if job.status is IndexJobStatus.RUNNING:
                job.status = IndexJobStatus.PENDING
                job.lease_until = None
            dispatcher.enqueue(job.id)
    await engine.dispose()
    return len(jobs)


async def enqueue_pending(settings: Settings) -> int:
    return await enqueue_recoverable_jobs(settings, DramatiqDispatcher(settings))


def main() -> None:
    settings = Settings()  # type: ignore[call-arg]
    asyncio.run(enqueue_pending(settings))


if __name__ == "__main__":
    main()
