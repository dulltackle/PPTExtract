from pptextract.ingestion import convert_presentation
from tests.support.synthetic_pptx import build_minimal_presentation


def test_presentation_conversion_registers_hidden_pages_without_processing_them() -> None:
    source = build_minimal_presentation()

    default = convert_presentation(source)
    enabled = convert_presentation(source, enabled_hidden_page_numbers=frozenset({2}))

    states = [
        (page.source.page_number, page.source.hidden, page.content is not None)
        for page in default
    ]
    assert states == [
        (1, False, True),
        (2, True, False),
    ]
    assert enabled[1].source.page_number == 2
    assert enabled[1].content is not None
    assert enabled[1].content.titles == ("公开合成隐藏页",)
    assert enabled[1].fingerprint is not None
