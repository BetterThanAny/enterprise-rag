"""Add a separate semantic embedding vector without reinterpreting legacy hashes."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "20260717_0006"
down_revision: str | None = "20260716_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "chunks",
        "embedding",
        existing_type=Vector(16),
        nullable=True,
    )
    op.add_column("chunks", sa.Column("semantic_embedding", Vector(384), nullable=True))
    op.create_check_constraint(
        "ck_chunks_exactly_one_embedding",
        "chunks",
        "num_nonnulls(embedding, semantic_embedding) = 1",
    )
    op.create_index(
        "ix_chunks_semantic_embedding_hnsw",
        "chunks",
        ["semantic_embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"semantic_embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    semantic_rows = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM chunks WHERE semantic_embedding IS NOT NULL")
    )
    if semantic_rows:
        raise RuntimeError(
            "Refusing destructive downgrade while semantic embedding rows exist; "
            "rebuild them with the deterministic provider first"
        )
    op.drop_index("ix_chunks_semantic_embedding_hnsw", table_name="chunks")
    op.drop_constraint("ck_chunks_exactly_one_embedding", "chunks", type_="check")
    op.drop_column("chunks", "semantic_embedding")
    op.alter_column(
        "chunks",
        "embedding",
        existing_type=Vector(16),
        nullable=False,
    )
