from __future__ import annotations

import pymupdf
import pytest

from enterprise_rag_core.indexing import (
    ChunkDraft,
    DeterministicEmbeddingStub,
    DeterministicIndexingError,
    FastEmbedEmbeddingProvider,
    calculate_retry_delay,
    chunk_document,
    chunk_text,
    parse_document,
)


@pytest.mark.parametrize(
    ("filename", "content", "expected"),
    [
        ("policy.txt", b"first\r\nsecond", "first\nsecond"),
        ("policy.md", b"# Title\n\nBody", "# Title\n\nBody"),
    ],
)
def test_text_and_markdown_are_parsed_and_cleaned(
    filename: str,
    content: bytes,
    expected: str,
) -> None:
    assert parse_document(filename, content) == expected


def test_pdf_is_parsed_with_real_pymupdf() -> None:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text(  # pyright: ignore[reportUnknownMemberType]
        (72, 72), "Enterprise retrieval policy"
    )
    content = document.tobytes()  # pyright: ignore[reportUnknownMemberType]
    document.close()

    parsed = parse_document("policy.pdf", content)

    assert "Enterprise retrieval policy" in parsed


@pytest.mark.parametrize(
    ("filename", "content", "code"),
    [
        ("invalid.txt", b"\xff\xfe", "invalid_utf8"),
        ("empty.md", b" \n\n ", "empty_document"),
        ("unsupported.docx", b"data", "unsupported_file_type"),
    ],
)
def test_deterministic_parse_failures_are_explicit(
    filename: str,
    content: bytes,
    code: str,
) -> None:
    with pytest.raises(DeterministicIndexingError) as caught:
        parse_document(filename, content)

    assert caught.value.code == code


def test_chunking_is_deterministic_and_preserves_overlap() -> None:
    assert chunk_text("abcdefghij", chunk_size=6, overlap=2) == ["abcdef", "efghij"]
    assert chunk_text("short", chunk_size=6, overlap=2) == ["short"]


def test_embedding_stub_is_deterministic_but_not_constant() -> None:
    provider = DeterministicEmbeddingStub(dimensions=16)

    first = provider.embed_documents(["tenant policy", "different text"])
    repeated = provider.embed_documents(["tenant policy"])

    assert len(first) == 2
    assert len(first[0]) == 16
    assert first[0] == repeated[0]
    assert first[0] != first[1]
    assert provider.embed_query("tenant policy") == first[0]
    assert provider.version == "deterministic-sha256-v1"
    assert provider.is_semantic is False


class RecordingFastEmbedModel:
    def __init__(self) -> None:
        self.inputs: list[list[str]] = []

    def embed(self, texts: list[str], *, batch_size: int) -> list[list[float]]:
        self.inputs.append(list(texts))
        return [[float(index + 1)] * 384 for index, _ in enumerate(texts)]


def test_fastembed_provider_separates_query_and_passage_encoding() -> None:
    model = RecordingFastEmbedModel()
    provider = FastEmbedEmbeddingProvider(
        model_name="BAAI/bge-small-en-v1.5",
        cache_dir="~/.cache/enterprise-rag/fastembed",
        batch_size=8,
        model=model,
    )

    documents = provider.embed_documents(["retention policy", "incident response"])
    query = provider.embed_query("how long are records retained?")

    assert model.inputs == [
        ["passage: retention policy", "passage: incident response"],
        ["query: how long are records retained?"],
    ]
    assert len(documents) == 2
    assert len(documents[0]) == len(query) == 384
    assert provider.version == "fastembed:BAAI/bge-small-en-v1.5"
    assert provider.is_semantic is True


def test_retry_delay_is_exponential_and_capped() -> None:
    assert calculate_retry_delay(1, base_seconds=2, max_seconds=10) == 2
    assert calculate_retry_delay(2, base_seconds=2, max_seconds=10) == 4
    assert calculate_retry_delay(10, base_seconds=2, max_seconds=10) == 10


def test_markdown_chunks_preserve_heading_hierarchy() -> None:
    chunks = chunk_document(
        "handbook.md",
        b"# Security\n\nIntro\n\n## Retention\n\nKeep records for seven years.",
        chunk_size=100,
        overlap=10,
    )

    assert [chunk.heading_path for chunk in chunks] == ["Security", "Security > Retention"]
    assert chunks[0].page_number is None
    assert chunks[1].content == "Keep records for seven years."


def test_heading_only_markdown_remains_indexable() -> None:
    chunks = chunk_document(
        "heading.md",
        b"# Security",
        chunk_size=100,
        overlap=10,
    )

    assert chunks == [ChunkDraft(content="Security", heading_path="Security")]


def test_pdf_chunks_preserve_one_based_page_numbers() -> None:
    document = pymupdf.open()
    first = document.new_page()
    first.insert_text((72, 72), "First page policy")  # pyright: ignore[reportUnknownMemberType]
    second = document.new_page()
    second.insert_text((72, 72), "Second page policy")  # pyright: ignore[reportUnknownMemberType]
    content = document.tobytes()  # pyright: ignore[reportUnknownMemberType]
    document.close()

    chunks = chunk_document("policy.pdf", content, chunk_size=100, overlap=10)

    assert [chunk.page_number for chunk in chunks] == [1, 2]
    assert [chunk.content for chunk in chunks] == ["First page policy", "Second page policy"]
