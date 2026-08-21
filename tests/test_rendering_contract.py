from __future__ import annotations

import subprocess
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from PIL import Image

from pptextract.pptx_projection import (
    MAX_XML_PART_BYTES,
    PackageLimitError,
    list_source_pages,
)
from pptextract.rendering import (
    DockerRenderingToolchain,
    audit_source_fonts,
    render_standard_pages,
)
from tests.support.synthetic_pptx import (
    build_minimal_presentation,
    build_public_contract_presentation,
)

RENDERING_IMAGE = "pptextract/document-toolchain:1"


def _rendering_image_is_available() -> bool:
    return (
        subprocess.run(
            ["docker", "image", "inspect", RENDERING_IMAGE],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


def test_render_entry_validates_package_limits_before_explicit_page_selection() -> None:
    source = build_minimal_presentation()
    output = BytesIO()
    with ZipFile(BytesIO(source)) as package, ZipFile(
        output, "w", compression=ZIP_DEFLATED
    ) as rewritten:
        for entry in package.infolist():
            content = package.read(entry.filename)
            if entry.filename == "ppt/presentation.xml":
                content = b" " * (MAX_XML_PART_BYTES + 1)
            rewritten.writestr(entry, content)

    with pytest.raises(PackageLimitError, match="XML part"):
        render_standard_pages(
            output.getvalue(),
            pages=(),
            toolchain=DockerRenderingToolchain(RENDERING_IMAGE),
        )


@pytest.mark.skipif(not _rendering_image_is_available(), reason="缺少锁定的渲染契约镜像")
def test_standard_render_uses_144_dpi_and_preserves_source_page_size() -> None:
    source = build_minimal_presentation()

    rendered = render_standard_pages(
        source,
        toolchain=DockerRenderingToolchain(RENDERING_IMAGE),
    )

    assert len(rendered) == 1
    assert (
        rendered[0].page_number,
        rendered[0].media_type,
        rendered[0].dpi,
        rendered[0].width_px,
        rendered[0].height_px,
    ) == (1, "image/png", 144, 1921, 1080)
    assert rendered[0].data.startswith(b"\x89PNG\r\n\x1a\n")


@pytest.mark.skipif(not _rendering_image_is_available(), reason="缺少锁定的渲染契约镜像")
def test_missing_source_font_produces_page_scoped_rendering_warning() -> None:
    warnings = audit_source_fonts(
        build_public_contract_presentation(),
        DockerRenderingToolchain(RENDERING_IMAGE),
    )

    assert [
        (warning.code, warning.page_number, warning.font_family) for warning in warnings
    ] == [("missing_font", 9, "PPTExtract Missing Contract Font")]


@pytest.mark.skipif(not _rendering_image_is_available(), reason="缺少锁定的渲染契约镜像")
def test_representative_visual_pages_render_as_nonempty_standard_pngs() -> None:
    source = build_public_contract_presentation()
    pages = list_source_pages(source)

    rendered = render_standard_pages(
        source,
        pages=(pages[3], pages[4], pages[8]),
        toolchain=DockerRenderingToolchain(RENDERING_IMAGE),
    )

    assert [page.page_number for page in rendered] == [4, 5, 9]
    assert [(page.width_px, page.height_px) for page in rendered] == [(1921, 1080)] * 3
    assert all(
        len(Image.open(BytesIO(page.data)).convert("RGB").getcolors(maxcolors=2_100_000) or ()) > 2
        for page in rendered
    )
