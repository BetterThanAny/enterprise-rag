from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace
from typing import ClassVar
from uuid import uuid4

import dramatiq
import pytest

from enterprise_rag_core.models import IndexJobStatus
from enterprise_rag_worker import broker as broker_module
from enterprise_rag_worker import dispatcher as dispatcher_module
from enterprise_rag_worker import run_job as run_job_module


class FakePipeline:
    status = IndexJobStatus.SUCCEEDED
    retry_delay_seconds: int | None = None
    seen_job_ids: ClassVar[list[object]] = []

    def __init__(self, settings: object) -> None:
        self.settings = settings

    async def process(self, job_id: object) -> SimpleNamespace:
        self.seen_job_ids.append(job_id)
        return SimpleNamespace(
            status=self.status,
            retry_delay_seconds=self.retry_delay_seconds,
        )


def test_run_job_entrypoint_accepts_terminal_status_and_rejects_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(run_job_module, "IndexingPipeline", FakePipeline)
    monkeypatch.setattr(run_job_module, "Settings", lambda: object())
    monkeypatch.setattr(sys, "argv", ["run_job", str(job_id)])

    FakePipeline.status = IndexJobStatus.SUCCEEDED
    run_job_module.main()
    assert FakePipeline.seen_job_ids[-1] == job_id

    FakePipeline.status = IndexJobStatus.PENDING
    with pytest.raises(SystemExit) as raised:
        run_job_module.main()
    assert raised.value.code == 2


def test_dramatiq_actor_retries_only_recoverable_statuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(broker_module, "configure_broker", lambda: object())
    sys.modules.pop("enterprise_rag_worker.tasks", None)
    tasks_module = importlib.import_module("enterprise_rag_worker.tasks")
    monkeypatch.setattr(tasks_module, "IndexingPipeline", FakePipeline)
    monkeypatch.setattr(tasks_module, "Settings", lambda: object())
    job_id = uuid4()

    FakePipeline.status = IndexJobStatus.SUCCEEDED
    FakePipeline.retry_delay_seconds = None
    tasks_module.process_index_job.fn(str(job_id))

    FakePipeline.status = IndexJobStatus.PENDING
    FakePipeline.retry_delay_seconds = 3
    with pytest.raises(dramatiq.Retry) as raised:
        tasks_module.process_index_job.fn(str(job_id))
    assert raised.value.delay == 3_000


def test_dispatcher_configures_broker_and_serializes_job_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured: list[object] = []
    sent: list[str] = []

    def send(value: str) -> None:
        sent.append(value)

    fake_actor = SimpleNamespace(send=send)
    fake_tasks = SimpleNamespace(process_index_job=fake_actor)

    def configure(settings: object) -> None:
        configured.append(settings)

    monkeypatch.setattr(
        dispatcher_module,
        "configure_broker",
        configure,
    )
    monkeypatch.setitem(sys.modules, "enterprise_rag_worker.tasks", fake_tasks)
    settings = object()
    job_id = uuid4()

    dispatcher = dispatcher_module.DramatiqDispatcher(settings)  # type: ignore[arg-type]
    dispatcher.enqueue(job_id)

    assert configured == [settings]
    assert sent == [str(job_id)]


def test_broker_is_configured_once_and_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[dict[str, object]] = []
    registered: list[object] = []

    class FakeRedisBroker:
        def __init__(self, **kwargs: object) -> None:
            created.append(kwargs)

    monkeypatch.setattr(broker_module, "RedisBroker", FakeRedisBroker)
    monkeypatch.setattr(broker_module.dramatiq, "set_broker", registered.append)
    monkeypatch.setattr(broker_module, "_broker", None)
    settings = SimpleNamespace(redis_url="redis://example.invalid:6379/0")

    first = broker_module.configure_broker(settings)  # type: ignore[arg-type]
    second = broker_module.configure_broker(settings)  # type: ignore[arg-type]

    assert first is second
    assert len(created) == 1
    assert created[0]["url"] == settings.redis_url
    assert registered == [first]
