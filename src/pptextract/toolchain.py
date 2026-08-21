from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import asdict, dataclass
from importlib import metadata, resources
from typing import Any

from pptextract.rendering import (
    PDF_EXPORT_FILTER,
    RENDER_PLATFORM,
    STANDARD_RENDER_DPI,
    DockerRenderingToolchain,
)


class ToolchainMismatch(RuntimeError):
    """实际第三方构建与锁定契约不一致。"""


@dataclass(frozen=True, slots=True)
class ToolchainContract:
    schema_version: int
    anydoc_distribution: str
    anydoc_version: str
    rendering_image_id: str
    libreoffice_version: str
    poppler_version: str
    fontconfig_version: str
    font_packages: tuple[tuple[str, str], ...]
    system_packages: tuple[tuple[str, str], ...]
    render_dpi: int
    render_format: str
    pdf_export_filter: str


@dataclass(frozen=True, slots=True)
class ToolchainReport:
    anydoc_distribution: str
    anydoc_version: str
    rendering_image_id: str
    libreoffice_version: str
    poppler_version: str
    fontconfig_version: str
    font_packages: tuple[tuple[str, str], ...]
    system_packages: tuple[tuple[str, str], ...]
    render_dpi: int
    render_format: str
    pdf_export_filter: str


def load_toolchain_contract() -> ToolchainContract:
    contract_file = resources.files("pptextract").joinpath("document_toolchain.json")
    payload = json.loads(contract_file.read_text(encoding="utf-8"))
    return _contract_from_dict(payload)


def probe_toolchain(toolchain: DockerRenderingToolchain) -> ToolchainReport:
    """探测当前实际加载的转换与渲染构建。"""
    image_id = _run_output(
        ["docker", "image", "inspect", "--format", "{{.Id}}", toolchain.image]
    )
    libreoffice = _container_output(toolchain, "libreoffice", "--version")
    poppler = _container_output(toolchain, "pdftoppm", "-v")
    fontconfig = _container_output(toolchain, "fc-list", "--version")
    package_lines = _container_output(
        toolchain,
        "dpkg-query",
        "-W",
        "-f=${Package}=${Version}\\n",
        "fonts-liberation2",
        "fonts-noto-cjk",
        "libreoffice-core-nogui",
        "libreoffice-impress-nogui",
        "poppler-utils",
    )
    parsed_packages: list[tuple[str, str]] = []
    for line in package_lines.splitlines():
        name, separator, version = line.partition("=")
        if not separator:
            raise ToolchainMismatch(f"无法解析字体包版本：{line}")
        parsed_packages.append((name, version))
    packages = dict(parsed_packages)
    font_packages = tuple(
        sorted((name, version) for name, version in packages.items() if name.startswith("fonts-"))
    )
    system_packages = tuple(
        sorted(
            (name, version)
            for name, version in packages.items()
            if not name.startswith("fonts-")
        )
    )
    return ToolchainReport(
        anydoc_distribution="firecrawl-anydoc",
        anydoc_version=metadata.version("firecrawl-anydoc"),
        rendering_image_id=image_id,
        libreoffice_version=libreoffice.removeprefix("LibreOffice "),
        poppler_version=poppler.splitlines()[0].removeprefix("pdftoppm version "),
        fontconfig_version=fontconfig.removeprefix("fontconfig version "),
        font_packages=font_packages,
        system_packages=system_packages,
        render_dpi=STANDARD_RENDER_DPI,
        render_format="png",
        pdf_export_filter=PDF_EXPORT_FILTER,
    )


def verify_toolchain_contract(report: ToolchainReport, contract: ToolchainContract) -> None:
    """逐字段校验实际构建；任何偏差都阻止门禁。"""
    mismatches: list[str] = []
    fields = (
        "anydoc_distribution",
        "anydoc_version",
        "rendering_image_id",
        "libreoffice_version",
        "poppler_version",
        "fontconfig_version",
        "font_packages",
        "system_packages",
        "render_dpi",
        "render_format",
        "pdf_export_filter",
    )
    for field in fields:
        expected = getattr(contract, field)
        actual = getattr(report, field)
        if actual != expected:
            label = "firecrawl-anydoc" if field.startswith("anydoc_") else field
            mismatches.append(f"{label}：期望 {expected!r}，实际 {actual!r}")
    if mismatches:
        raise ToolchainMismatch("第三方契约门禁失败：\n- " + "\n- ".join(mismatches))


def _contract_from_dict(payload: dict[str, Any]) -> ToolchainContract:
    if payload.get("schema_version") != 1:
        raise ValueError("不支持的文档工具链契约版本")
    font_packages = tuple(sorted(payload["font_packages"].items()))
    system_packages = tuple(sorted(payload["system_packages"].items()))
    return ToolchainContract(
        schema_version=payload["schema_version"],
        anydoc_distribution=payload["anydoc_distribution"],
        anydoc_version=payload["anydoc_version"],
        rendering_image_id=payload["rendering_image_id"],
        libreoffice_version=payload["libreoffice_version"],
        poppler_version=payload["poppler_version"],
        fontconfig_version=payload["fontconfig_version"],
        font_packages=font_packages,
        system_packages=system_packages,
        render_dpi=payload["render_dpi"],
        render_format=payload["render_format"],
        pdf_export_filter=payload["pdf_export_filter"],
    )


def _container_output(
    toolchain: DockerRenderingToolchain, entrypoint: str, *arguments: str
) -> str:
    return _run_output(
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
        ]
    )


def _run_output(command: list[str]) -> str:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ToolchainMismatch(f"第三方契约探测失败：{error}") from error
    output = (completed.stdout or completed.stderr).strip()
    if completed.returncode != 0:
        raise ToolchainMismatch(f"第三方契约探测失败：{output}")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="校验 PPTExtract 文档工具链契约")
    parser.add_argument(
        "--render-image",
        default=os.environ.get("PPTEXTRACT_RENDER_IMAGE", ""),
        help="包含 LibreOffice、Poppler 与字体包的不可变镜像引用",
    )
    arguments = parser.parse_args()
    if not arguments.render_image:
        parser.error("必须通过 --render-image 或 PPTEXTRACT_RENDER_IMAGE 指定渲染镜像")
    contract = load_toolchain_contract()
    report = probe_toolchain(DockerRenderingToolchain(arguments.render_image))
    verify_toolchain_contract(report, contract)
    print(json.dumps(asdict(report), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
