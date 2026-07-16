"""Add reconstructable OpenTelemetry and generation usage fields."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260716_0005"
down_revision: str | None = "20260715_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("retrieval_traces", sa.Column("trace_id", sa.String(32), nullable=True))
    op.add_column("retrieval_traces", sa.Column("span_id", sa.String(16), nullable=True))
    op.add_column("retrieval_traces", sa.Column("rerank_span_id", sa.String(16)))
    op.execute("UPDATE retrieval_traces SET trace_id = md5(id::text)")
    op.execute(
        "UPDATE retrieval_traces SET span_id = substr(md5(id::text || '-retrieval'), 1, 16)"
    )
    op.alter_column("retrieval_traces", "trace_id", nullable=False)
    op.alter_column("retrieval_traces", "span_id", nullable=False)
    op.create_index("ix_retrieval_traces_trace_id", "retrieval_traces", ["trace_id"])

    op.add_column("generation_traces", sa.Column("request_id", sa.String(128), nullable=True))
    op.add_column("generation_traces", sa.Column("trace_id", sa.String(32), nullable=True))
    op.add_column("generation_traces", sa.Column("span_id", sa.String(16), nullable=True))
    op.add_column("generation_traces", sa.Column("provider_span_id", sa.String(16)))
    op.add_column("generation_traces", sa.Column("ttft_ms", sa.Float()))
    op.add_column("generation_traces", sa.Column("duration_ms", sa.Float()))
    op.add_column(
        "generation_traces",
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "generation_traces",
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "generation_traces",
        sa.Column(
            "usage_source",
            sa.String(20),
            server_default="unavailable",
            nullable=False,
        ),
    )
    op.add_column(
        "generation_traces",
        sa.Column(
            "estimated_cost_usd",
            sa.Numeric(18, 8),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column(
        "generation_traces",
        sa.Column("provider_attempts", sa.Integer(), server_default="0", nullable=False),
    )
    op.execute(
        """
        UPDATE generation_traces AS generation
        SET
            request_id = 'migrated-' || generation.id::text,
            trace_id = retrieval.trace_id,
            span_id = substr(md5(generation.id::text || '-generation'), 1, 16),
            provider_attempts = CASE WHEN generation.status = 'abstained' THEN 0 ELSE 1 END
        FROM retrieval_traces AS retrieval
        WHERE retrieval.id = generation.retrieval_trace_id
          AND retrieval.tenant_id = generation.tenant_id
        """
    )
    op.alter_column("generation_traces", "request_id", nullable=False)
    op.alter_column("generation_traces", "trace_id", nullable=False)
    op.alter_column("generation_traces", "span_id", nullable=False)
    op.create_index("ix_generation_traces_trace_id", "generation_traces", ["trace_id"])


def downgrade() -> None:
    op.drop_index("ix_generation_traces_trace_id", table_name="generation_traces")
    for column in (
        "provider_attempts",
        "estimated_cost_usd",
        "usage_source",
        "output_tokens",
        "input_tokens",
        "duration_ms",
        "ttft_ms",
        "provider_span_id",
        "span_id",
        "trace_id",
        "request_id",
    ):
        op.drop_column("generation_traces", column)
    op.drop_index("ix_retrieval_traces_trace_id", table_name="retrieval_traces")
    op.drop_column("retrieval_traces", "rerank_span_id")
    op.drop_column("retrieval_traces", "span_id")
    op.drop_column("retrieval_traces", "trace_id")
