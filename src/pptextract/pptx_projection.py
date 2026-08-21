from __future__ import annotations

import posixpath
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

PRESENTATION_NAMESPACE = "http://schemas.openxmlformats.org/presentationml/2006/main"
OFFICE_REL_NAMESPACE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NAMESPACE = "http://schemas.openxmlformats.org/package/2006/relationships"
MAX_PACKAGE_ENTRIES = 10_000
MAX_TOTAL_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_ENTRY_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
MAX_XML_PART_BYTES = 8 * 1024 * 1024


class PackageLimitError(ValueError):
    """PPTX 在进入 AnyDoc 前越过固定资源上限。"""


@dataclass(frozen=True, slots=True)
class SourcePage:
    """源文档中一页的可追溯清单项。"""

    page_number: int
    source_slide_id: int
    relationship_id: str
    source_part: str
    hidden: bool
    source_document_sha256: str


def list_source_pages(pptx_bytes: bytes) -> tuple[SourcePage, ...]:
    """按 ``sldIdLst`` 顺序读取源页清单。"""
    source_document_sha256 = sha256(pptx_bytes).hexdigest()
    try:
        with ZipFile(BytesIO(pptx_bytes)) as package:
            _validate_package_limits(package)
            presentation = ElementTree.fromstring(package.read("ppt/presentation.xml"))
            relationships = _presentation_relationships(package)
            pages: list[SourcePage] = []
            slide_ids = presentation.findall(f".//{{{PRESENTATION_NAMESPACE}}}sldId")
            for page_number, slide_id in enumerate(slide_ids, start=1):
                relationship_id = slide_id.attrib[f"{{{OFFICE_REL_NAMESPACE}}}id"]
                source_part = relationships[relationship_id]
                slide = ElementTree.fromstring(package.read(source_part))
                pages.append(
                    SourcePage(
                        page_number=page_number,
                        source_slide_id=int(slide_id.attrib["id"]),
                        relationship_id=relationship_id,
                        source_part=source_part,
                        hidden=slide.attrib.get("show", "1").lower() in {"0", "false", "off", "no"},
                        source_document_sha256=source_document_sha256,
                    )
                )
    except PackageLimitError:
        raise
    except (BadZipFile, KeyError, ElementTree.ParseError, ValueError) as error:
        raise ValueError("无效的 PPTX 源页清单") from error
    return tuple(pages)


def project_page(pptx_bytes: bytes, page: SourcePage, *, force_visible: bool = False) -> bytes:
    """复制完整 OPC 包，并只收窄演示文稿的页入口。"""
    if page not in list_source_pages(pptx_bytes):
        raise ValueError("源页不属于该 PPTX")

    output = BytesIO()
    with ZipFile(BytesIO(pptx_bytes)) as source, ZipFile(output, mode="w") as projected:
        _validate_package_limits(source)
        for entry in source.infolist():
            content = source.read(entry.filename)
            if entry.filename == "ppt/presentation.xml":
                presentation = ElementTree.fromstring(content)
                slide_id_list = presentation.find(f"{{{PRESENTATION_NAMESPACE}}}sldIdLst")
                if slide_id_list is None:
                    raise ValueError("PPTX 缺少源页清单")
                for slide_id in tuple(slide_id_list):
                    relationship_id = slide_id.attrib.get(f"{{{OFFICE_REL_NAMESPACE}}}id")
                    if relationship_id != page.relationship_id:
                        slide_id_list.remove(slide_id)
                content = ElementTree.tostring(
                    presentation, encoding="utf-8", xml_declaration=True
                )
            elif force_visible and entry.filename == page.source_part:
                slide = ElementTree.fromstring(content)
                slide.attrib.pop("show", None)
                content = ElementTree.tostring(slide, encoding="utf-8", xml_declaration=True)
            projected.writestr(entry, content)
    return output.getvalue()


def _presentation_relationships(package: ZipFile) -> dict[str, str]:
    root = ElementTree.fromstring(package.read("ppt/_rels/presentation.xml.rels"))
    relationships: dict[str, str] = {}
    for relationship in root.findall(f"{{{PACKAGE_REL_NAMESPACE}}}Relationship"):
        if relationship.attrib.get("TargetMode") == "External":
            continue
        target = relationship.attrib["Target"]
        relationships[relationship.attrib["Id"]] = _resolve_part_uri(
            "ppt/presentation.xml", target
        )
    return relationships


def _validate_package_limits(package: ZipFile) -> None:
    entries = package.infolist()
    if len(entries) > MAX_PACKAGE_ENTRIES:
        raise PackageLimitError("PPTX entry count exceeds the fixed limit")
    if len({entry.filename for entry in entries}) != len(entries):
        raise PackageLimitError("PPTX contains duplicate package parts")
    total_size = 0
    for entry in entries:
        total_size += entry.file_size
        if entry.file_size > MAX_ENTRY_UNCOMPRESSED_BYTES:
            raise PackageLimitError("PPTX part exceeds the fixed size limit")
        if entry.filename.lower().endswith((".xml", ".rels")) and (
            entry.file_size > MAX_XML_PART_BYTES
        ):
            raise PackageLimitError("PPTX XML part exceeds the fixed size limit")
    if total_size > MAX_TOTAL_UNCOMPRESSED_BYTES:
        raise PackageLimitError("PPTX expanded size exceeds the fixed total limit")


def _resolve_part_uri(source_part: str, target: str) -> str:
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("OPC 内部关系包含无效 URI")
    decoded = unquote(parsed.path)
    if "\\" in decoded or "\x00" in decoded:
        raise ValueError("OPC 内部关系包含无效路径")
    if decoded.startswith("/"):
        candidate = decoded.lstrip("/")
    else:
        candidate = posixpath.join(posixpath.dirname(source_part), decoded)
    normalized = posixpath.normpath(candidate)
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        raise ValueError("OPC 内部关系越出包根")
    return normalized
