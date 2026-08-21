from pptextract.conversion import convert_page
from pptextract.pptx_projection import list_source_pages
from tests.support.synthetic_pptx import build_conversion_presentation


def test_conversion_normalizes_text_and_repeated_image_references() -> None:
    source, image_bytes = build_conversion_presentation()
    page = list_source_pages(source)[0]

    content = convert_page(source, page)

    assert content.titles == ("公开图片页",)
    assert content.body == ("第一段正文",)
    assert [
        (image.reference_index, image.alt_text, image.media_type, image.data)
        for image in content.images
    ] == [
        (0, "第一处图片引用", "image/png", image_bytes),
        (1, "第二处图片引用", "image/png", image_bytes),
    ]


def test_conversion_normalizes_merged_table_grid_and_speaker_notes() -> None:
    source, _ = build_conversion_presentation()
    page = list_source_pages(source)[0]

    content = convert_page(source, page)

    table = content.tables[0]
    assert (table.kind, table.header_rows, len(table.grid), len(table.grid[0])) == (
        "data",
        1,
        3,
        3,
    )
    assert (table.grid[0][0].cell.text, table.grid[0][0].cell.col_span) == ("合并表头", 2)
    assert (table.grid[0][1].kind, table.grid[0][1].origin_row, table.grid[0][1].origin_col) == (
        "covered",
        0,
        0,
    )
    assert content.speaker_notes == ("公开演讲者备注",)
