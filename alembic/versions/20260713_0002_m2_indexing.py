"""Add immutable document versions, chunks, vectors, and recoverable job state."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "20260713_0002"
down_revision: str | None = "20260713_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def timestamps() -> list[sa.Column[object]]:
    return [
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
    ]


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "document_versions",
        sa.Column("id", UUID, nullable=False),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("document_id", UUID, nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "ready",
                "failed",
                "superseded",
                name="document_version_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        *timestamps(),
        sa.CheckConstraint(
            "char_length(checksum) = 64",
            name="ck_document_versions_checksum_length",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_document_versions_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["documents.tenant_id", "documents.id"],
            name="fk_document_versions_tenant_document",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_document_versions"),
        sa.UniqueConstraint(
            "tenant_id",
            "document_id",
            "id",
            name="uq_document_versions_tenant_document_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "document_id",
            "version_number",
            name="uq_document_versions_number",
        ),
        sa.UniqueConstraint("object_key", name="uq_document_versions_object_key"),
    )
    op.create_index(
        "uq_document_versions_current",
        "document_versions",
        ["tenant_id", "document_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )
    op.execute(
        """
        INSERT INTO document_versions (
            id, tenant_id, document_id, version_number, filename, object_key,
            checksum, status, is_current, created_at, updated_at
        )
        SELECT
            id, tenant_id, id, 1, filename, object_key, checksum,
            CASE
                WHEN status = 'ready' THEN 'ready'
                WHEN status = 'failed' THEN 'failed'
                ELSE 'pending'
            END,
            status = 'ready', created_at, updated_at
        FROM documents
        """
    )
    op.create_table(
        "chunks",
        sa.Column("id", UUID, nullable=False),
        sa.Column("tenant_id", UUID, nullable=False),
        sa.Column("document_id", UUID, nullable=False),
        sa.Column("version_id", UUID, nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_checksum", sa.String(length=64), nullable=False),
        sa.Column("embedding", Vector(16), nullable=False),
        *timestamps(),
        sa.CheckConstraint(
            "char_length(content_checksum) = 64",
            name="ck_chunks_content_checksum_length",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_chunks_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id", "version_id"],
            [
                "document_versions.tenant_id",
                "document_versions.document_id",
                "document_versions.id",
            ],
            name="fk_chunks_tenant_document_version",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_chunks"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_chunks_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "version_id",
            "ordinal",
            name="uq_chunks_version_ordinal",
        ),
    )
    op.create_index(
        "ix_chunks_tenant_document_version",
        "chunks",
        ["tenant_id", "document_id", "version_id"],
    )

    op.add_column("index_jobs", sa.Column("version_id", UUID, nullable=True))
    op.add_column("index_jobs", sa.Column("payload_checksum", sa.String(64), nullable=True))
    op.add_column(
        "index_jobs",
        sa.Column(
            "action",
            sa.Enum(
                "index",
                "rebuild",
                name="index_job_action",
                native_enum=False,
                create_constraint=True,
            ),
            server_default="index",
            nullable=False,
        ),
    )
    op.add_column(
        "index_jobs",
        sa.Column(
            "stage",
            sa.Enum(
                "queued",
                "parse",
                "embedding",
                "database_write",
                "complete",
                name="index_job_stage",
                native_enum=False,
                create_constraint=True,
            ),
            server_default="queued",
            nullable=False,
        ),
    )
    op.add_column(
        "index_jobs",
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "index_jobs",
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.add_column("index_jobs", sa.Column("lease_until", sa.DateTime(timezone=True)))
    op.add_column("index_jobs", sa.Column("started_at", sa.DateTime(timezone=True)))
    op.add_column("index_jobs", sa.Column("finished_at", sa.DateTime(timezone=True)))
    op.execute(
        """
        UPDATE index_jobs AS jobs
        SET version_id = jobs.document_id, payload_checksum = documents.checksum
        FROM documents
        WHERE documents.id = jobs.document_id AND documents.tenant_id = jobs.tenant_id
        """
    )
    op.alter_column("index_jobs", "version_id", nullable=False)
    op.alter_column("index_jobs", "payload_checksum", nullable=False)
    op.create_check_constraint(
        "ck_index_jobs_payload_checksum_length",
        "index_jobs",
        "char_length(payload_checksum) = 64",
    )
    op.create_foreign_key(
        "fk_index_jobs_tenant_document_version",
        "index_jobs",
        "document_versions",
        ["tenant_id", "document_id", "version_id"],
        ["tenant_id", "document_id", "id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_index_jobs_tenant_document_version",
        "index_jobs",
        type_="foreignkey",
    )
    op.drop_constraint("ck_index_jobs_payload_checksum_length", "index_jobs", type_="check")
    for column in (
        "finished_at",
        "started_at",
        "lease_until",
        "available_at",
        "attempts",
        "stage",
        "action",
        "payload_checksum",
        "version_id",
    ):
        op.drop_column("index_jobs", column)
    op.drop_index("ix_chunks_tenant_document_version", table_name="chunks")
    op.drop_table("chunks")
    op.drop_index("uq_document_versions_current", table_name="document_versions")
    op.drop_table("document_versions")
