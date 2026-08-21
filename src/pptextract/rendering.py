from __future__ import annotations

import os
import struct
import subprocess
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from xml.etree import ElementTree
from zipfile import ZipFile

from pptextract.pptx_projection import (
    PRESENTATION_NAMESPACE,
    SourcePage,
    list_source_pages,
    project_page,
)

STANDARD_RENDER_DPI = 144
RENDER_PLATFORM = "linux/amd64"
EMU_PER_INCH = 914_400
DRAWING_NAMESPACE = "http://schemas.openxmlformats.org/drawingml/2006/main"
PDF_EXPORT_FILTER = (
    'pdf:impress_pdf_Export:{"ExportHiddenSlides":{"type":"boolean","value":"false"},'
    '"ExportNotes":{"type":"boolean","value":"false"}}'
)


@dataclass(frozen=True, slots=True)
class DockerRenderingToolchain:
    """以不可变容器镜像运行 LibreOffice 与 Poppler。"""

    image: str


@dataclass(frozen=True, slots=True)
class StandardPageRender:
    page_number: int
    media_type: str
    dpi: int
    width_px: int
    height_px: int
    data: bytes


@dataclass(frozen=True, slots=True)
class RenderingWarning:
    code: str
    page_number: int
    font_family: str


def audit_source_fonts(
    pptx_bytes: bytes, toolchain: DockerRenderingToolchain
) -> tuple[RenderingWarning, ...]:
    """在渲染前按源页审计显式字体，并报告容器中缺失的字体。"""
    installed_output = _docker_output(toolchain, "fc-list", "-f", "%{family}\n")
    installed = {
        family.strip().casefold()
        for line in installed_output.splitlines()
        for family in line.split(",")
        if family.strip()
    }
    warnings: list[RenderingWarning] = []
    with ZipFile(BytesIO(pptx_bytes)) as package:
        for page in list_source_pages(pptx_bytes):
            slide = ElementTree.fromstring(package.read(page.source_part))
            page_fonts: set[str] = set()
            for element in slide.iter():
                if element.tag not in {
                    f"{{{DRAWING_NAMESPACE}}}rPr",
                    f"{{{DRAWING_NAMESPACE}}}defRPr",
                    f"{{{DRAWING_NAMESPACE}}}endParaRPr",
                    f"{{{DRAWING_NAMESPACE}}}latin",
                    f"{{{DRAWING_NAMESPACE}}}ea",
                    f"{{{DRAWING_NAMESPACE}}}cs",
                }:
                    continue
                family = element.attrib.get("typeface", "").strip()
                if family and not family.startswith("+"):
                    page_fonts.add(family)
            warnings.extend(
                RenderingWarning("missing_font", page.page_number, family)
                for family in sorted(page_fonts)
                if family.casefold() not in installed
            )
    return tuple(warnings)


def render_standard_pages(
    pptx_bytes: bytes,
    *,
    toolchain: DockerRenderingToolchain,
    pages: tuple[SourcePage, ...] | None = None,
) -> tuple[StandardPageRender, ...]:
    """逐页生成标准页渲染结果；默认跳过隐藏页。"""
    manifest = list_source_pages(pptx_bytes)
    if pages is None:
        selected = tuple(page for page in manifest if not page.hidden)
    else:
        unknown_pages = tuple(page for page in pages if page not in manifest)
        if unknown_pages:
            raise ValueError("显式渲染页不属于当前 PPTX 的源页清单")
        selected = pages
    expected_width, expected_height = _expected_pixel_size(pptx_bytes)
    rendered: list[StandardPageRender] = []

    with TemporaryDirectory(prefix="pptextract-render-") as temporary:
        root = Path(temporary)
        for page in selected:
            page_root = root / f"page-{page.page_number}"
            page_root.mkdir()
            (page_root / "source.pptx").write_bytes(
                project_page(pptx_bytes, page, force_visible=True)
            )
            _render_with_docker(page_root, toolchain)
            png = (page_root / "page.png").read_bytes()
            width, height = _png_size(png)
            if abs(width - expected_width) > 1 or abs(height - expected_height) > 1:
                raise RuntimeError(
                    "标准页渲染结果未保持源页物理尺寸："
                    f"期望 {expected_width}x{expected_height}，实际 {width}x{height}"
                )
            rendered.append(
                StandardPageRender(
                    page_number=page.page_number,
                    media_type="image/png",
                    dpi=STANDARD_RENDER_DPI,
                    width_px=width,
                    height_px=height,
                    data=png,
                )
            )
    return tuple(rendered)


def _render_with_docker(root: Path, toolchain: DockerRenderingToolchain) -> None:
    mount = f"{root}:/work"
    common = [
        "docker",
        "run",
        "--rm",
        "--platform",
        RENDER_PLATFORM,
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "256",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--volume",
        mount,
        "--env",
        "HOME=/tmp",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,noexec,size=256m",
    ]
    _run(
        [
            *common,
            "--entrypoint",
            "libreoffice",
            toolchain.image,
            "--headless",
            "--convert-to",
            PDF_EXPORT_FILTER,
            "--outdir",
            "/work",
            "/work/source.pptx",
        ]
    )
    _run(
        [
            *common,
            "--entrypoint",
            "pdftoppm",
            toolchain.image,
            "-f",
            "1",
            "-singlefile",
            "-png",
            "-r",
            str(STANDARD_RENDER_DPI),
            "/work/source.pdf",
            "/work/page",
        ]
    )


def _docker_output(
    toolchain: DockerRenderingToolchain, entrypoint: str, *arguments: str
) -> str:
    completed = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--platform",
            RENDER_PLATFORM,
            "--entrypoint",
            entrypoint,
            toolchain.image,
            *arguments,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"渲染工具执行失败：{detail}")
    return (completed.stdout or completed.stderr).strip()


def _run(command: list[str]) -> None:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"渲染工具执行失败：{detail}")


def _expected_pixel_size(pptx_bytes: bytes) -> tuple[int, int]:
    with ZipFile(BytesIO(pptx_bytes)) as package:
        presentation = ElementTree.fromstring(package.read("ppt/presentation.xml"))
    size = presentation.find(f"{{{PRESENTATION_NAMESPACE}}}sldSz")
    if size is None:
        raise ValueError("PPTX 缺少页面物理尺寸")
    width = round(int(size.attrib["cx"]) / EMU_PER_INCH * STANDARD_RENDER_DPI)
    height = round(int(size.attrib["cy"]) / EMU_PER_INCH * STANDARD_RENDER_DPI)
    return width, height


def _png_size(data: bytes) -> tuple[int, int]:
    if len(data) < 24 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("Poppler 未生成有效 PNG")
    return struct.unpack(">II", data[16:24])
