from __future__ import annotations

import hashlib
import json
import os
import posixpath
import struct
import subprocess
from dataclasses import dataclass
from importlib import resources
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
RELATIONSHIPS_NAMESPACE = "http://schemas.openxmlformats.org/package/2006/relationships"
PDF_EXPORT_FILTER = (
    'pdf:impress_pdf_Export:{"ExportHiddenSlides":{"type":"boolean","value":"false"},'
    '"ExportNotes":{"type":"boolean","value":"false"}}'
)


def render_configuration_version(image: str) -> str:
    """返回覆盖全部标准渲染规则的稳定配置版本。"""
    contract_file = resources.files("pptextract").joinpath("document_toolchain.json")
    contract = json.loads(contract_file.read_text(encoding="utf-8"))
    material = json.dumps(
        {
            "image": image,
            "platform": RENDER_PLATFORM,
            "dpi": STANDARD_RENDER_DPI,
            "filter": PDF_EXPORT_FILTER,
            "locked_toolchain": contract,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"render-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:16]}"


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
    font_families: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RenderingWarning:
    code: str
    page_number: int
    font_family: str | None = None
    replacement_font: str | None = None
    timeline_count: int | None = None


def audit_animation_timelines(pptx_bytes: bytes) -> tuple[RenderingWarning, ...]:
    """按源页记录会被标准静态渲染扁平化的动画时间线。"""
    warnings: list[RenderingWarning] = []
    timing_tag = f"{{{PRESENTATION_NAMESPACE}}}timing"
    with ZipFile(BytesIO(pptx_bytes)) as package:
        for page in list_source_pages(pptx_bytes):
            slide = ElementTree.fromstring(package.read(page.source_part))
            timeline_count = sum(1 for element in slide.iter() if element.tag == timing_tag)
            if timeline_count:
                warnings.append(
                    RenderingWarning(
                        code="animation_flattened",
                        page_number=page.page_number,
                        timeline_count=timeline_count,
                    )
                )
    return tuple(warnings)


def audit_source_fonts(
    pptx_bytes: bytes,
    toolchain: DockerRenderingToolchain,
    *,
    rendered_fonts: dict[int, tuple[str, ...]] | None = None,
) -> tuple[RenderingWarning, ...]:
    """按源页审计直接及版式继承字体，并报告渲染容器中的缺失字体。"""
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
            page_fonts = _page_source_fonts(package, page.source_part)
            actual = (
                None
                if rendered_fonts is None
                else rendered_fonts.get(page.page_number, ())
            )
            if actual is None:
                at_risk = [
                    family
                    for family in sorted(page_fonts)
                    if family.casefold() not in installed
                ]
                remaining_actual = None
            else:
                direct_matches = {
                    family
                    for family in page_fonts
                    if any(_font_names_match(family, candidate) for candidate in actual)
                }
                unmatched_actual = tuple(
                    candidate
                    for candidate in actual
                    if not any(
                        _font_names_match(candidate, family)
                        for family in direct_matches
                    )
                )
                at_risk = [
                    family for family in sorted(page_fonts) if family not in direct_matches
                ]
                # PDF 同时包含源字体和额外字体，表示同一页发生了逐字形回退；
                # 即使字体家族已安装，也必须产生带实际回退家族的警告。
                if unmatched_actual:
                    at_risk.extend(sorted(direct_matches))
                remaining_actual = unmatched_actual
            replacements = _font_replacements(
                at_risk,
                toolchain=toolchain,
                actual=remaining_actual,
            )
            for family in at_risk:
                warnings.append(
                    RenderingWarning(
                        code="missing_font",
                        page_number=page.page_number,
                        font_family=family,
                        replacement_font=replacements[family],
                    )
                )
    return tuple(warnings)


def _font_replacements(
    source_fonts: list[str],
    *,
    toolchain: DockerRenderingToolchain,
    actual: tuple[str, ...] | None,
) -> dict[str, str | None]:
    predicted = {
        family: _docker_output(toolchain, "fc-match", "--format", "%{family}", family)
        .strip()
        .split(",", 1)[0]
        .strip()
        for family in source_fonts
    }
    if actual is None:
        return {family: candidate or None for family, candidate in predicted.items()}

    candidates = list(actual)
    replacements: dict[str, str | None] = {family: None for family in source_fonts}
    used: set[str] = set()
    for family in source_fonts:
        match = next(
            (
                candidate
                for candidate in candidates
                if candidate not in used and _font_names_match(candidate, predicted[family])
            ),
            None,
        )
        if match is not None:
            replacements[family] = match
            used.add(match)
    unresolved = [family for family in source_fonts if replacements[family] is None]
    remaining = [candidate for candidate in candidates if candidate not in used]
    if len(unresolved) == len(remaining):
        for family, candidate in zip(unresolved, remaining, strict=True):
            replacements[family] = candidate
    elif remaining:
        actual_fallbacks = ", ".join(remaining)
        for family in unresolved:
            replacements[family] = actual_fallbacks
    return replacements


def _font_names_match(left: str, right: str) -> bool:
    normalized_left = _normalized_font_name(left)
    normalized_right = _normalized_font_name(right)
    return normalized_left in normalized_right or normalized_right in normalized_left


def _page_source_fonts(package: ZipFile, slide_part: str) -> set[str]:
    """按实际文字、占位符角色与脚本解析 theme 继承字体。"""
    slide = ElementTree.fromstring(package.read(slide_part))
    layout_part = _related_part(package, slide_part, "/slideLayout")
    if layout_part is not None:
        master_part = _related_part(package, layout_part, "/slideMaster")
    else:
        master_part = None
    theme_part = (
        _related_part(package, master_part, "/theme") if master_part is not None else None
    )
    theme_fonts = _theme_font_map(package, theme_part)

    fonts: set[str] = set()
    processed_runs: set[int] = set()

    def collect_run(run: ElementTree.Element, scope: str) -> None:
        processed_runs.add(id(run))
        text = "".join(
            element.text or "" for element in run.iter(f"{{{DRAWING_NAMESPACE}}}t")
        )
        if not text.strip():
            return
        run_properties = run.find(f"{{{DRAWING_NAMESPACE}}}rPr")
        explicit = _explicit_run_fonts(run_properties, theme_fonts)
        if explicit:
            fonts.update(explicit)
            return
        for script in _text_scripts(text):
            family = theme_fonts.get(f"+{scope}-{script}".casefold())
            if family:
                fonts.add(family)

    for shape in slide.iter(f"{{{PRESENTATION_NAMESPACE}}}sp"):
        placeholder = shape.find(f".//{{{PRESENTATION_NAMESPACE}}}ph")
        placeholder_type = "" if placeholder is None else placeholder.attrib.get("type", "")
        scope = "mj" if placeholder_type in {"title", "ctrTitle", "subTitle"} else "mn"
        for run_tag in ("r", "fld"):
            for run in shape.iter(f"{{{DRAWING_NAMESPACE}}}{run_tag}"):
                collect_run(run, scope)
    # 表格单元格等 DrawingML 文本不在 p:sp 中，默认走正文（minor）主题。
    for run_tag in ("r", "fld"):
        for run in slide.iter(f"{{{DRAWING_NAMESPACE}}}{run_tag}"):
            if id(run) not in processed_runs:
                collect_run(run, "mn")
    return fonts


def _explicit_run_fonts(
    run_properties: ElementTree.Element | None, theme_fonts: dict[str, str]
) -> set[str]:
    if run_properties is None:
        return set()
    fonts: set[str] = set()
    generic = run_properties.attrib.get("typeface", "").strip()
    candidates = [generic] if generic else []
    candidates.extend(
        element.attrib.get("typeface", "").strip()
        for element in run_properties
        if element.tag
        in {
            f"{{{DRAWING_NAMESPACE}}}latin",
            f"{{{DRAWING_NAMESPACE}}}ea",
            f"{{{DRAWING_NAMESPACE}}}cs",
        }
    )
    for candidate in candidates:
        if not candidate:
            continue
        resolved = theme_fonts.get(candidate.casefold()) if candidate.startswith("+") else candidate
        if resolved:
            fonts.add(resolved)
    return fonts


def _text_scripts(text: str) -> set[str]:
    scripts: set[str] = set()
    for character in text:
        codepoint = ord(character)
        if 0x3400 <= codepoint <= 0x9FFF:
            scripts.add("hans")
        elif 0x3040 <= codepoint <= 0x30FF:
            scripts.add("jpan")
        elif 0xAC00 <= codepoint <= 0xD7AF:
            scripts.add("hang")
        elif 0x0600 <= codepoint <= 0x06FF:
            scripts.add("arab")
        elif 0x0590 <= codepoint <= 0x05FF:
            scripts.add("hebr")
        elif 0x0E00 <= codepoint <= 0x0E7F:
            scripts.add("thai")
        elif character.isalpha():
            scripts.add("lt")
    return scripts or {"lt"}


def _related_part(package: ZipFile, source_part: str, relationship_suffix: str) -> str | None:
    directory, filename = posixpath.split(source_part)
    relationships_part = posixpath.join(directory, "_rels", f"{filename}.rels")
    if relationships_part not in package.namelist():
        return None
    root = ElementTree.fromstring(package.read(relationships_part))
    for relationship in root.findall(f"{{{RELATIONSHIPS_NAMESPACE}}}Relationship"):
        if not relationship.attrib.get("Type", "").endswith(relationship_suffix):
            continue
        target = relationship.attrib.get("Target", "")
        return posixpath.normpath(posixpath.join(directory, target))
    return None


def _theme_font_map(package: ZipFile, theme_part: str | None) -> dict[str, str]:
    if theme_part is None or theme_part not in package.namelist():
        return {}
    root = ElementTree.fromstring(package.read(theme_part))
    mapping: dict[str, str] = {}
    for scope, prefix in (("majorFont", "+mj"), ("minorFont", "+mn")):
        scope_element = root.find(f".//{{{DRAWING_NAMESPACE}}}{scope}")
        if scope_element is None:
            continue
        for script, suffix in (("latin", "lt"), ("ea", "ea"), ("cs", "cs")):
            element = scope_element.find(f"{{{DRAWING_NAMESPACE}}}{script}")
            family = "" if element is None else element.attrib.get("typeface", "").strip()
            if family:
                mapping[f"{prefix}-{suffix}"] = family
        for element in scope_element.findall(f"{{{DRAWING_NAMESPACE}}}font"):
            script = element.attrib.get("script", "").strip().casefold()
            family = element.attrib.get("typeface", "").strip()
            if script and family:
                mapping[f"{prefix}-{script}"] = family
    return mapping


def audit_rendering_warnings(
    pptx_bytes: bytes,
    toolchain: DockerRenderingToolchain,
    *,
    rendered_fonts: dict[int, tuple[str, ...]] | None = None,
) -> tuple[RenderingWarning, ...]:
    """汇总源字体、实际 PDF 字体替代与动画静态扁平化风险。"""
    warnings = (
        *audit_source_fonts(pptx_bytes, toolchain, rendered_fonts=rendered_fonts),
        *audit_animation_timelines(pptx_bytes),
    )
    return tuple(
        sorted(
            warnings,
            key=lambda warning: (
                warning.page_number,
                warning.code,
                warning.font_family or "",
            ),
        )
    )


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
            font_families = _render_with_docker(page_root, toolchain)
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
                    font_families=font_families,
                )
            )
    return tuple(rendered)


def _render_with_docker(root: Path, toolchain: DockerRenderingToolchain) -> tuple[str, ...]:
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
            "-env:UserInstallation=file:///tmp/libreoffice-profile",
            "--headless",
            "--convert-to",
            PDF_EXPORT_FILTER,
            "--outdir",
            "/work",
            "/work/source.pptx",
        ]
    )
    font_output = _run_output(
        [
            *common,
            "--entrypoint",
            "pdffonts",
            toolchain.image,
            "/work/source.pdf",
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
    return _parse_pdf_font_families(font_output)


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


def _run_output(command: list[str]) -> str:
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
    return completed.stdout


def _parse_pdf_font_families(output: str) -> tuple[str, ...]:
    families: set[str] = set()
    for line in output.splitlines()[2:]:
        columns = line.split()
        if not columns:
            continue
        name = columns[0]
        if "+" in name and len(name.split("+", 1)[0]) == 6:
            name = name.split("+", 1)[1]
        families.add(name.replace("-", " "))
    return tuple(sorted(families))


def _normalized_font_name(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


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
