from __future__ import annotations

import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_fresh_machine_toolchain_pins_every_host_command_dependency() -> None:
    mise_config = tomllib.loads(
        (PROJECT_ROOT / ".mise.toml").read_text(encoding="utf-8")
    )
    tools = mise_config["tools"]

    assert tools["python"]
    assert tools["uv"]
    assert tools["gitleaks"]

    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert f"ghcr.io/astral-sh/uv:{tools['uv']}" in dockerfile

    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    assert "mise install" in readme
    assert "mise exec -- uv sync --frozen" in readme
    assert "mise exec -- uv run python scripts/demo.py" in readme


def test_ci_waits_only_for_long_running_stateful_services() -> None:
    workflow = (PROJECT_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "docker compose up -d --wait postgres redis minio" in workflow
    assert "docker compose run --rm --no-deps minio-init" in workflow
    assert "postgres redis minio minio-init --wait" not in workflow


def test_ci_enforces_fault_coverage_and_secret_gates() -> None:
    workflow = (PROJECT_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "tests/fault" in workflow
    assert "--cov-fail-under=88" in workflow
    assert "enterprise_rag_core/documents.py" in workflow
    assert "enterprise_rag_worker/run_job.py" in workflow
    assert "--fail-under=95" in workflow
    assert "gitleaks dir . --redact --no-banner" in workflow


def test_portfolio_positioning_keeps_evidence_boundaries_explicit() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    operations = (PROJECT_ROOT / "docs/operations.md").read_text(encoding="utf-8")

    assert "local backend portfolio project" in readme
    assert "offline in-memory dense retrieval evaluation" in readme
    assert "deterministic 16-dimensional test embeddings" in readme
    assert "public history begins with a consolidated M1-M5 snapshot" in readme
    assert "20260717_0006 (head)" in operations
