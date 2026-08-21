from io import BytesIO
from xml.etree import ElementTree
from zipfile import ZIP_DEFLATED, ZipFile

import anydoc
import pytest

from pptextract.pptx_projection import (
    MAX_XML_PART_BYTES,
    PackageLimitError,
    list_source_pages,
    project_page,
)
from tests.support.synthetic_pptx import (
    build_conversion_presentation,
    build_minimal_presentation,
)


def test_source_page_manifest_preserves_order_references_and_hidden_state() -> None:
    pages = list_source_pages(build_minimal_presentation())

    assert [
        (
            page.page_number,
            page.source_slide_id,
            page.relationship_id,
            page.source_part,
            page.hidden,
        )
        for page in pages
    ] == [
        (1, 256, "rId7", "ppt/slides/slide1.xml", False),
        (2, 257, "rId8", "ppt/slides/slide2.xml", True),
    ]


def test_single_page_projection_isolates_adjacent_page_content() -> None:
    source = build_minimal_presentation()
    first, second = list_source_pages(source)

    first_markdown = anydoc.to_markdown_bytes(project_page(source, first), "pptx")
    second_markdown = anydoc.to_markdown_bytes(project_page(source, second), "pptx")

    assert "公开合成页一" in first_markdown
    assert "公开合成隐藏页" not in first_markdown
    assert "公开合成隐藏页" in second_markdown
    assert "公开合成页一" not in second_markdown


def test_page_handle_from_another_document_is_rejected() -> None:
    source_page = list_source_pages(build_minimal_presentation())[0]
    other_document, _ = build_conversion_presentation()

    with pytest.raises(ValueError, match="不属于该 PPTX"):
        project_page(other_document, source_page)


def test_source_page_manifest_accepts_absolute_and_percent_encoded_part_uris() -> None:
    source = build_minimal_presentation()
    output = BytesIO()
    relationship_namespace = (
        "http://schemas.openxmlformats.org/package/2006/relationships"
    )
    with ZipFile(BytesIO(source)) as package, ZipFile(output, "w") as rewritten:
        for entry in package.infolist():
            content = package.read(entry.filename)
            if entry.filename == "ppt/_rels/presentation.xml.rels":
                relationships = ElementTree.fromstring(content)
                slide_relationships = [
                    relationship
                    for relationship in relationships.findall(
                        f"{{{relationship_namespace}}}Relationship"
                    )
                    if relationship.attrib["Type"].endswith("/slide")
                ]
                slide_relationships[0].set("Target", "/ppt/%73lides/slide1.xml")
                content = ElementTree.tostring(
                    relationships, encoding="utf-8", xml_declaration=True
                )
            rewritten.writestr(entry, content)

    assert list_source_pages(output.getvalue())[0].source_part == "ppt/slides/slide1.xml"


def test_source_page_manifest_rejects_oversized_xml_before_parsing() -> None:
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
        list_source_pages(output.getvalue())
