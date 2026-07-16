from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from threading import Lock
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

_configuration_lock = Lock()
_configured = False

HTTP_REQUESTS = Counter(
    "enterprise_rag_http_requests_total",
    "Completed HTTP requests.",
    ("method", "route", "status"),
)
HTTP_DURATION = Histogram(
    "enterprise_rag_http_request_duration_seconds",
    "HTTP request duration, excluding streamed body consumption.",
    ("method", "route"),
)
RETRIEVAL_DURATION = Histogram(
    "enterprise_rag_retrieval_duration_seconds",
    "Tenant-filtered retrieval and optional rerank duration.",
    ("mode", "rerank"),
)
GENERATION_TTFT = Histogram(
    "enterprise_rag_generation_ttft_seconds",
    "Time from provider invocation to first text delta.",
    ("provider", "model"),
)
GENERATION_DURATION = Histogram(
    "enterprise_rag_generation_duration_seconds",
    "Provider generation duration.",
    ("provider", "model", "status"),
)
GENERATION_RUNS = Counter(
    "enterprise_rag_generation_runs_total",
    "Generation terminal outcomes, including application-level failures over SSE.",
    ("provider", "model", "status"),
)
GENERATION_INPUT_TOKENS = Counter(
    "enterprise_rag_generation_input_tokens_total",
    "Provider-reported or explicitly estimated input tokens.",
    ("provider", "model", "source"),
)
GENERATION_OUTPUT_TOKENS = Counter(
    "enterprise_rag_generation_output_tokens_total",
    "Provider-reported or explicitly estimated output tokens.",
    ("provider", "model", "source"),
)
GENERATION_COST = Counter(
    "enterprise_rag_generation_estimated_cost_usd_total",
    "Estimated generation cost using versioned deployment rates.",
    ("provider", "model"),
)
PROVIDER_RETRIES = Counter(
    "enterprise_rag_provider_retries_total",
    "Provider retries before any output was emitted.",
    ("provider", "reason"),
)


def configure_telemetry(
    *,
    service_name: str,
    service_version: str,
    environment: str,
    otlp_endpoint: str | None,
) -> None:
    global _configured
    if _configured:
        return
    with _configuration_lock:
        if _configured:
            return
        provider = TracerProvider(
            resource=Resource.create(
                {
                    "service.name": service_name,
                    "service.version": service_version,
                    "deployment.environment.name": environment,
                }
            )
        )
        if otlp_endpoint:
            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint))
            )
        trace.set_tracer_provider(provider)
        _configured = True


def ensure_telemetry() -> None:
    configure_telemetry(
        service_name="enterprise-rag",
        service_version="unknown",
        environment="unknown",
        otlp_endpoint=None,
    )


@contextmanager
def start_span(
    name: str,
    attributes: dict[str, Any] | None = None,
    *,
    parent: Span | None = None,
) -> Generator[Span]:
    ensure_telemetry()
    tracer = trace.get_tracer("enterprise_rag")
    parent_context = trace.set_span_in_context(parent) if parent is not None else None
    span = tracer.start_span(name, context=parent_context, attributes=attributes)
    try:
        yield span
    finally:
        span.end()


def span_identifiers(span: Span) -> tuple[str, str]:
    context = span.get_span_context()
    return f"{context.trace_id:032x}", f"{context.span_id:016x}"


def prometheus_payload() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
