from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import anydoc

from pptextract.pptx_projection import SourcePage, project_page


@dataclass(frozen=True, slots=True)
class NormalizedImage:
    """按页内出现顺序保存的一次图片引用。"""

    reference_index: int
    alt_text: str
    media_type: str
    origin_part: str
    data: bytes


@dataclass(frozen=True, slots=True)
class NormalizedTableCell:
    text: str
    col_span: int
    row_span: int


@dataclass(frozen=True, slots=True)
class NormalizedTableSlot:
    kind: Literal["origin", "covered"]
    cell: NormalizedTableCell | None
    origin_row: int | None
    origin_col: int | None


@dataclass(frozen=True, slots=True)
class NormalizedTable:
    kind: Literal["data", "layout"]
    header_rows: int
    grid: tuple[tuple[NormalizedTableSlot, ...], ...]


@dataclass(frozen=True, slots=True)
class NormalizedPageContent:
    """AnyDoc 对象之外的项目内部页内容模型。"""

    titles: tuple[str, ...]
    body: tuple[str, ...]
    tables: tuple[NormalizedTable, ...]
    images: tuple[NormalizedImage, ...]
    speaker_notes: tuple[str, ...] = ()


def convert_page(pptx_bytes: bytes, page: SourcePage) -> NormalizedPageContent:
    """隔离转换一页，并在适配边界内立即归一化。"""
    document = anydoc.to_document(project_page(pptx_bytes, page), "pptx")
    titles: list[str] = []
    body: list[str] = []
    tables: list[NormalizedTable] = []
    images: list[NormalizedImage] = []
    speaker_notes: list[str] = []

    for block in document.blocks:
        if block.kind in {"heading", "paragraph"}:
            text = _inline_text(block.content or ())
            if text:
                (titles if block.kind == "heading" else body).append(text)
            _collect_images(block.content or (), document.assets, images)
        elif block.kind == "table" and block.table:
            tables.append(_normalize_table(block.table))
        elif block.kind == "list" and block.list:
            list_text = _list_text(block.list)
            if list_text:
                body.append(list_text)
        elif block.kind == "block_quote" and block.blocks:
            note = _blocks_text(block.blocks)
            if note:
                speaker_notes.append(note)

    return NormalizedPageContent(
        titles=tuple(titles),
        body=tuple(body),
        tables=tuple(tables),
        images=tuple(images),
        speaker_notes=tuple(speaker_notes),
    )


def _inline_text(inlines: list[anydoc.Inline] | tuple[()]) -> str:
    fragments: list[str] = []
    for inline in inlines:
        if inline.kind == "text" and inline.text:
            fragments.append(inline.text)
        elif inline.kind == "line_break":
            fragments.append("\n")
        elif inline.kind == "link" and inline.content:
            fragments.append(_inline_text(inline.content))
    return "".join(fragments).strip()


def _collect_images(
    inlines: list[anydoc.Inline] | tuple[()],
    assets: list[anydoc.Asset],
    images: list[NormalizedImage],
) -> None:
    for inline in inlines:
        if inline.kind == "image" and inline.source and inline.source.kind == "asset":
            asset_id = inline.source.asset_id
            if asset_id is None or asset_id < 0 or asset_id >= len(assets):
                raise ValueError("AnyDoc 返回了无效的图片资产引用")
            asset = assets[asset_id]
            images.append(
                NormalizedImage(
                    reference_index=len(images),
                    alt_text=inline.alt or "",
                    media_type=asset.media_type,
                    origin_part=asset.origin_part,
                    data=asset.data,
                )
            )
        elif inline.kind == "link" and inline.content:
            _collect_images(inline.content, assets, images)


def _normalize_table(table: anydoc.Table) -> NormalizedTable:
    rows: list[tuple[NormalizedTableSlot, ...]] = []
    for row in table.grid:
        slots: list[NormalizedTableSlot] = []
        for slot in row:
            cell = slot.cell
            normalized_cell = None
            if cell is not None:
                normalized_cell = NormalizedTableCell(
                    text=_blocks_text(cell.blocks),
                    col_span=cell.col_span,
                    row_span=cell.row_span,
                )
            slots.append(
                NormalizedTableSlot(
                    kind=slot.kind,
                    cell=normalized_cell,
                    origin_row=slot.origin_row,
                    origin_col=slot.origin_col,
                )
            )
        rows.append(tuple(slots))
    return NormalizedTable(kind=table.kind, header_rows=table.header_rows, grid=tuple(rows))


def _blocks_text(blocks: list[anydoc.Block]) -> str:
    fragments: list[str] = []
    for block in blocks:
        if block.kind in {"heading", "paragraph"}:
            text = _inline_text(block.content or ())
            if text:
                fragments.append(text)
        elif block.kind == "block_quote" and block.blocks:
            nested = _blocks_text(block.blocks)
            if nested:
                fragments.append(nested)
        elif block.kind == "list" and block.list:
            nested = _list_text(block.list)
            if nested:
                fragments.append(nested)
    return "\n".join(fragments)


def _list_text(list_block: anydoc.List) -> str:
    return "\n".join(
        item_text for item in list_block.items if (item_text := _blocks_text(item.blocks))
    )
