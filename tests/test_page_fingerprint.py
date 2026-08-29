from pptextract.conversion import NormalizedImage, NormalizedPageContent, NormalizedSourcePart
from pptextract.fingerprint import canonical_fingerprint_input, fingerprint_page


def test_page_fingerprint_has_versioned_canonical_input_and_keeps_duplicate_images() -> None:
    content = NormalizedPageContent(
        titles=("标题",),
        body=("正文",),
        tables=(),
        images=(
            NormalizedImage(0, "甲图", "image/png", "ppt/media/image1.png", b"shared"),
            NormalizedImage(1, "乙图", "image/png", "ppt/media/image1.png", b"shared"),
        ),
        speaker_notes=("备注",),
    )

    canonical = canonical_fingerprint_input(content)
    fingerprint = fingerprint_page(content)

    assert canonical == (
        '{"body":["正文"],"fingerprint_version":2,"images":['
        '{"alt_text":"甲图","sha256":'
        '"a4d26868017c0ccffe2efe50944ef4211834660cca834c6e9f86dec6a88246fa"},'
        '{"alt_text":"乙图","sha256":'
        '"a4d26868017c0ccffe2efe50944ef4211834660cca834c6e9f86dec6a88246fa"}'
        '],"source_order":[],"speaker_notes":["备注"],"tables":[],"titles":["标题"]}'
    ).encode()
    assert (fingerprint.version, fingerprint.sha256) == (
        2,
        "44f67e398ac2fc764043cacdb7897c8c851f2468337d295687c9155f61b56987",
    )


def test_page_fingerprint_distinguishes_cross_kind_source_order() -> None:
    common = {
        "titles": ("标题",),
        "body": ("正文",),
        "tables": (),
        "images": (
            NormalizedImage(0, "图示", "image/png", "ppt/media/image1.png", b"image"),
        ),
    }
    body_first = NormalizedPageContent(
        **common,
        source_order=(NormalizedSourcePart("body", 0), NormalizedSourcePart("image_alt", 0)),
    )
    image_first = NormalizedPageContent(
        **common,
        source_order=(NormalizedSourcePart("image_alt", 0), NormalizedSourcePart("body", 0)),
    )

    assert fingerprint_page(body_first).sha256 != fingerprint_page(image_first).sha256
