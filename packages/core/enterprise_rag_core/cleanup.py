from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from enterprise_rag_core.config import Settings
from enterprise_rag_core.database import create_database_resources
from enterprise_rag_core.models import DocumentVersion
from enterprise_rag_core.storage import MinioObjectStorage


@dataclass(frozen=True)
class CleanupResult:
    scanned: int
    orphaned: int
    removed: int


class OrphanCleanupService:
    def __init__(self, settings: Settings) -> None:
        self.storage = MinioObjectStorage(settings)
        self.engine, self.session_factory = create_database_resources(settings.database_url)

    async def run(self, *, dry_run: bool = False) -> CleanupResult:
        async with self.session_factory() as session:
            referenced = set((await session.execute(select(DocumentVersion.object_key))).scalars())
        stored = await self.storage.list_keys("tenants/")
        orphaned = [key for key in stored if key not in referenced]
        if not dry_run:
            await self.storage.remove_many(orphaned)
        await self.engine.dispose()
        return CleanupResult(
            scanned=len(stored),
            orphaned=len(orphaned),
            removed=0 if dry_run else len(orphaned),
        )
