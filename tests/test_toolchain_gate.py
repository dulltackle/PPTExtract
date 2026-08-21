from __future__ import annotations

from dataclasses import replace

import pytest

from pptextract.rendering import DockerRenderingToolchain
from pptextract.toolchain import (
    ToolchainMismatch,
    load_toolchain_contract,
    probe_toolchain,
    verify_toolchain_contract,
)
from tests.test_rendering_contract import RENDERING_IMAGE, _rendering_image_is_available


@pytest.mark.skipif(not _rendering_image_is_available(), reason="缺少锁定的渲染契约镜像")
def test_actual_document_toolchain_matches_locked_contract() -> None:
    contract = load_toolchain_contract()
    report = probe_toolchain(DockerRenderingToolchain(RENDERING_IMAGE))

    verify_toolchain_contract(report, contract)

    assert report.anydoc_version == "0.1.9"
    assert report.libreoffice_version == "24.2.7.2 420(Build:2)"
    assert report.poppler_version == "24.02.0"
    assert report.font_packages == (
        ("fonts-liberation2", "1:2.1.5-3"),
        ("fonts-noto-cjk", "1:20230817+repack1-3"),
    )
    assert report.system_packages == (
        ("libreoffice-core-nogui", "4:24.2.7-0ubuntu0.24.04.6"),
        ("libreoffice-impress-nogui", "4:24.2.7-0ubuntu0.24.04.6"),
        ("poppler-utils", "24.02.0-1ubuntu9.9"),
    )


@pytest.mark.skipif(not _rendering_image_is_available(), reason="缺少锁定的渲染契约镜像")
def test_toolchain_upgrade_mismatch_blocks_the_gate() -> None:
    contract = load_toolchain_contract()
    report = probe_toolchain(DockerRenderingToolchain(RENDERING_IMAGE))

    with pytest.raises(ToolchainMismatch, match="firecrawl-anydoc"):
        verify_toolchain_contract(report, replace(contract, anydoc_version="9.9.9"))
