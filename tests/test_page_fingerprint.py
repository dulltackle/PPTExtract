from pptextract.conversion import NormalizedImage, NormalizedPageContent
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
        '{"body":["正文"],"fingerprint_version":1,"images":['
        '{"alt_text":"甲图","sha256":'
        '"a4d26868017c0ccffe2efe50944ef4211834660cca834c6e9f86dec6a88246fa"},'
        '{"alt_text":"乙图","sha256":'
        '"a4d26868017c0ccffe2efe50944ef4211834660cca834c6e9f86dec6a88246fa"}'
        '],"speaker_notes":["备注"],"tables":[],"titles":["标题"]}'
    ).encode()
    assert (fingerprint.version, fingerprint.sha256) == (
        1,
        "5517cb412230af0ff0c501c590b261d18d98d236ce0cd42dd8887bdd9d90a0dc",
    )
