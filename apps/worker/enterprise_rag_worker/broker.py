from __future__ import annotations

import dramatiq
from dramatiq.brokers.redis import RedisBroker

from enterprise_rag_core.config import Settings

_broker: RedisBroker | None = None


def configure_broker(settings: Settings | None = None) -> RedisBroker:
    global _broker
    if _broker is None:
        resolved_settings = settings or Settings()  # type: ignore[call-arg]
        _broker = RedisBroker(
            url=resolved_settings.redis_url,
            namespace="enterprise-rag",
            heartbeat_timeout=5_000,
            socket_connect_timeout=1,
            socket_timeout=1,
            retry_on_timeout=False,
            health_check_interval=1,
        )
        dramatiq.set_broker(_broker)
    return _broker
