from __future__ import annotations

import asyncio

import dramatiq

from enterprise_rag_core.config import Settings
from enterprise_rag_core.indexing import IndexingPipeline
from enterprise_rag_core.models import IndexJobStatus
from enterprise_rag_worker.broker import configure_broker

configure_broker()


@dramatiq.actor(max_retries=10, min_backoff=1_000, max_backoff=60_000)
def process_index_job(job_id: str) -> None:
    from uuid import UUID

    result = asyncio.run(IndexingPipeline(Settings()).process(UUID(job_id)))  # type: ignore[call-arg]
    if result.status in {IndexJobStatus.PENDING, IndexJobStatus.RUNNING}:
        delay_seconds = result.retry_delay_seconds or 1
        raise dramatiq.Retry(delay=delay_seconds * 1_000)
