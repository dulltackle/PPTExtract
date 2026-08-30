from __future__ import annotations

import subprocess
from pathlib import Path


def test_public_ci_cannot_skip_product_or_document_contract_gates() -> None:
    project_root = Path(__file__).resolve().parents[1]
    workflow = (project_root / ".github/workflows/document-contract.yml").read_text(
        encoding="utf-8"
    )

    assert "pull_request:" in workflow
    assert "push:" in workflow
    assert "paths:" not in workflow
    assert "paths-ignore:" not in workflow
    for bypass in ("continue-on-error:", "|| true", "if: false", "if: ${{ false }}"):
        assert bypass not in workflow
    for required_command in (
        "uv sync --locked --dev",
        "npm ci",
        "npx --no-install playwright install --with-deps chromium",
        "python -m pptextract.toolchain",
        "uv run mypy src/pptextract",
        "uv run ruff check src tests",
        "npm test",
        "npm run typecheck",
        "npm run build",
        "uv run pytest",
    ):
        assert required_command in workflow


def test_public_ci_cannot_read_or_track_private_local_presentations() -> None:
    project_root = Path(__file__).resolve().parents[1]
    workflow = (project_root / ".github/workflows/document-contract.yml").read_text(
        encoding="utf-8"
    )
    ignore_rules = (project_root / ".gitignore").read_text(encoding="utf-8")
    tracked = subprocess.run(
        ["git", "ls-files", "fixtures"],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()

    assert "fixtures" not in workflow
    assert tracked == ["fixtures/README.md"]
    for required_rule in (
        "/fixtures/*.pptx",
        "/fixtures/*.zip",
        "/fixtures/README.local.md",
    ):
        assert required_rule in ignore_rules
