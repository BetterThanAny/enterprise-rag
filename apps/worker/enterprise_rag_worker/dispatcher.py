from __future__ import annotations

from uuid import UUID

from enterprise_rag_core.config import Settings
from enterprise_rag_worker.broker import configure_broker


class DramatiqDispatcher:
    def __init__(self, settings: Settings) -> None:
        configure_broker(settings)

    def enqueue(self, job_id: UUID) -> None:
        from enterprise_rag_worker.tasks import process_index_job

        process_index_job.send(str(job_id))
