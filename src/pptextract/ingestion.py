from __future__ import annotations

from dataclasses import dataclass

from pptextract.conversion import NormalizedPageContent, convert_page
from pptextract.fingerprint import PageFingerprint, fingerprint_page
from pptextract.pptx_projection import SourcePage, list_source_pages


@dataclass(frozen=True, slots=True)
class ConvertedSourcePage:
    source: SourcePage
    content: NormalizedPageContent | None
    fingerprint: PageFingerprint | None


def convert_presentation(
    pptx_bytes: bytes,
    *,
    enabled_hidden_page_numbers: frozenset[int] = frozenset(),
) -> tuple[ConvertedSourcePage, ...]:
    """登记全部源页，并只处理默认可见或被明确启用的隐藏页。"""
    pages: list[ConvertedSourcePage] = []
    for source in list_source_pages(pptx_bytes):
        if source.hidden and source.page_number not in enabled_hidden_page_numbers:
            pages.append(ConvertedSourcePage(source=source, content=None, fingerprint=None))
            continue
        content = convert_page(pptx_bytes, source)
        pages.append(
            ConvertedSourcePage(
                source=source,
                content=content,
                fingerprint=fingerprint_page(content),
            )
        )
    return tuple(pages)
