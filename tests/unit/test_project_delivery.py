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

    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert f"ghcr.io/astral-sh/uv:{tools['uv']}" in dockerfile

    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    assert "mise install" in readme
    assert "mise exec -- uv sync --frozen" in readme
    assert "mise exec -- uv run python scripts/demo.py" in readme
