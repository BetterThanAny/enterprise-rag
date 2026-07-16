from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

from enterprise_rag_api.routers.generation import stream_until_disconnect


class DisconnectAfterFirstEvent:
    def __init__(self) -> None:
        self.calls = 0

    async def is_disconnected(self) -> bool:
        self.calls += 1
        return self.calls > 1


async def test_disconnect_cancels_upstream_and_closes_generator() -> None:
    released = asyncio.Event()
    cancelled = asyncio.Event()

    async def upstream() -> AsyncGenerator[str, None]:
        try:
            yield "first"
            await asyncio.Event().wait()
        finally:
            released.set()

    wrapped = stream_until_disconnect(
        DisconnectAfterFirstEvent(),
        upstream(),
        cancelled,
    )

    assert await anext(wrapped) == "first"
    try:
        await anext(wrapped)
    except StopAsyncIteration:
        pass

    assert cancelled.is_set()
    assert released.is_set()
