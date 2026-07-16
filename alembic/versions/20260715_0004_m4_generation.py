"""Add citation metadata and tenant-owned generation traces."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260715_0004"
down_revision: str | None = "20260713_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.add_column("chunks", sa.Column("page_number", sa.Integer(), nullable=True))
    op.add_column("chunks", sa.Column("heading_path", sa.String(length=1000), nullable=True))
    op.create_unique_constraint(
        "uq_retrieval_traces_tenant_id_id",
        "retrieval_traces",
        ["tenant_id", "id"],
    )
    op.create_table(
        "generation_traces",
        sa.Column("id", UUID, nullable=False),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("knowledge_base_id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=True),
        sa.Column("retrieval_trace_id", UUID, nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("rendered_prompt", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "running",
                "succeeded",
                "abstained",
                "cancelled",
                "failed",
                name="generation_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("citations", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("provider_config_version", sa.String(length=200), nullable=False),
        sa.Column("prompt_version", sa.String(length=200), nullable=False),
        sa.Column("retriever_version", sa.String(length=200), nullable=False),
        sa.Column("embedding_version", sa.String(length=200), nullable=False),
        sa.Column("reranker_version", sa.String(length=200), nullable=True),
        sa.Column("dataset_version", sa.String(length=200), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.String(length=2000), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_generation_traces_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "knowledge_base_id"],
            ["knowledge_bases.tenant_id", "knowledge_bases.id"],
            name="fk_generation_traces_tenant_knowledge_base",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "retrieval_trace_id"],
            ["retrieval_traces.tenant_id", "retrieval_traces.id"],
            name="fk_generation_traces_tenant_retrieval_trace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_generation_traces_user_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_generation_traces"),
    )
    op.create_index(
        "ix_generation_traces_tenant_created",
        "generation_traces",
        ["tenant_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_generation_traces_tenant_created", table_name="generation_traces")
    op.drop_table("generation_traces")
    op.drop_constraint(
        "uq_retrieval_traces_tenant_id_id",
        "retrieval_traces",
        type_="unique",
    )
    op.drop_column("chunks", "heading_path")
    op.drop_column("chunks", "page_number")
