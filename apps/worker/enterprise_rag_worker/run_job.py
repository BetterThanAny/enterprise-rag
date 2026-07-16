from __future__ import annotations

import argparse
import asyncio
from uuid import UUID

from enterprise_rag_core.config import Settings
from enterprise_rag_core.indexing import IndexingPipeline
from enterprise_rag_core.models import IndexJobStatus


def main() -> None:
    parser = argparse.ArgumentParser(description="Process one recoverable index job")
    parser.add_argument("job_id", type=UUID)
    arguments = parser.parse_args()
    result = asyncio.run(
        IndexingPipeline(Settings()).process(arguments.job_id)  # type: ignore[call-arg]
    )
    if result.status not in {IndexJobStatus.SUCCEEDED, IndexJobStatus.FAILED}:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
