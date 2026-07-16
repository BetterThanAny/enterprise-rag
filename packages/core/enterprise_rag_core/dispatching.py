from __future__ import annotations

from typing import Protocol
from uuid import UUID


class JobDispatcher(Protocol):
    def enqueue(self, job_id: UUID) -> None: ...


class RecordingDispatcher:
    """Test dispatcher that records enqueue operations without starting a worker."""

    def __init__(self) -> None:
        self.job_ids: list[UUID] = []

    def enqueue(self, job_id: UUID) -> None:
        self.job_ids.append(job_id)


class UnavailableDispatcher:
    def enqueue(self, job_id: UUID) -> None:
        del job_id
        raise RuntimeError("No background job dispatcher is configured")
