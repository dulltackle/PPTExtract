from __future__ import annotations

import subprocess
from io import BytesIO
from pathlib import Path
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
    audit_animation_timelines,
    audit_source_fonts,
    render_configuration_version,
    render_standard_pages,
)
from tests.support.synthetic_pptx import (
    build_installed_font_glyph_fallback_presentation,
    build_minimal_presentation,
    build_plain_text_presentation,
    build_public_contract_presentation,
    build_rendering_warning_presentation,
    build_table_font_presentation,
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


def test_render_configuration_version_includes_locked_toolchain_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = tmp_path / "document_toolchain.json"
    contract.write_text('{"rendering_image_id":"sha256:first"}', encoding="utf-8")
    monkeypatch.setattr("pptextract.rendering.resources.files", lambda _package: tmp_path)
    first = render_configuration_version(RENDERING_IMAGE)

    contract.write_text('{"rendering_image_id":"sha256:second"}', encoding="utf-8")

    assert render_configuration_version(RENDERING_IMAGE) != first


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

    assert (
        "missing_font",
        9,
        "PPTExtract Missing Contract Font",
    ) in [
        (warning.code, warning.page_number, warning.font_family) for warning in warnings
    ]
    assert any(warning.font_family == "宋体" for warning in warnings)


def test_animation_timeline_produces_static_flattening_warning() -> None:
    warnings = audit_animation_timelines(build_rendering_warning_presentation())

    assert len(warnings) == 1
    assert warnings[0].code == "animation_flattened"
    assert warnings[0].page_number == 2
    assert warnings[0].timeline_count == 1


def test_table_cell_explicit_font_is_included_in_source_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pptextract.rendering._docker_output",
        lambda _toolchain, entrypoint, *_arguments: (
            "Noto Sans\n" if entrypoint == "fc-list" else "Noto Sans"
        ),
    )

    warnings = audit_source_fonts(
        build_table_font_presentation(),
        DockerRenderingToolchain(RENDERING_IMAGE),
    )

    assert any(
        warning.font_family == "PPTExtract Missing Table Font" for warning in warnings
    )


def test_missing_font_records_actual_substitute(monkeypatch: pytest.MonkeyPatch) -> None:
    def docker_output(
        _toolchain: DockerRenderingToolchain, entrypoint: str, *arguments: str
    ) -> str:
        if entrypoint == "fc-list":
            return "Noto Sans\nLiberation Sans\n"
        assert entrypoint == "fc-match"
        return "Noto Sans"

    monkeypatch.setattr("pptextract.rendering._docker_output", docker_output)

    warnings = audit_source_fonts(
        build_rendering_warning_presentation(),
        DockerRenderingToolchain(RENDERING_IMAGE),
    )

    warning = next(
        item for item in warnings if item.font_family == "PPTExtract Missing Contract Font"
    )
    assert warning.replacement_font == "Noto Sans"


@pytest.mark.skipif(not _rendering_image_is_available(), reason="缺少锁定的渲染契约镜像")
def test_inherited_chinese_font_records_the_actual_pdf_substitute() -> None:
    source = build_plain_text_presentation()
    (rendered,) = render_standard_pages(
        source,
        toolchain=DockerRenderingToolchain(RENDERING_IMAGE),
    )

    warnings = audit_source_fonts(
        source,
        DockerRenderingToolchain(RENDERING_IMAGE),
        rendered_fonts={1: rendered.font_families},
    )

    inherited = next(warning for warning in warnings if warning.font_family == "宋体")
    assert inherited.replacement_font in rendered.font_families


@pytest.mark.skipif(not _rendering_image_is_available(), reason="缺少锁定的渲染契约镜像")
def test_installed_font_glyph_fallback_is_not_silently_ignored() -> None:
    source = build_installed_font_glyph_fallback_presentation()
    (rendered,) = render_standard_pages(
        source,
        toolchain=DockerRenderingToolchain(RENDERING_IMAGE),
    )

    warnings = audit_source_fonts(
        source,
        DockerRenderingToolchain(RENDERING_IMAGE),
        rendered_fonts={1: rendered.font_families},
    )

    fallback = next(warning for warning in warnings if warning.font_family == "Liberation Sans")
    assert fallback.replacement_font in rendered.font_families


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
