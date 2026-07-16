from __future__ import annotations

import argparse
import asyncio

from enterprise_rag_core.cleanup import OrphanCleanupService
from enterprise_rag_core.config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Remove unreferenced M2 objects from MinIO")
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args()
    result = asyncio.run(
        OrphanCleanupService(Settings()).run(dry_run=arguments.dry_run)  # type: ignore[call-arg]
    )
    print(f"scanned={result.scanned} orphaned={result.orphaned} removed={result.removed}")


if __name__ == "__main__":
    main()
