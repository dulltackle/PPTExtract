from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from pptextract.conversion import NormalizedPageContent, NormalizedTable

FINGERPRINT_VERSION = 1


@dataclass(frozen=True, slots=True)
class PageFingerprint:
    version: int
    sha256: str


def canonical_fingerprint_input(content: NormalizedPageContent) -> bytes:
    """生成页来源内容的版本化确定性序列化。"""
    payload = {
        "fingerprint_version": FINGERPRINT_VERSION,
        "titles": content.titles,
        "body": content.body,
        "tables": [_serialize_table(table) for table in content.tables],
        "images": [
            {
                "alt_text": image.alt_text,
                "sha256": hashlib.sha256(image.data).hexdigest(),
            }
            for image in content.images
        ],
        "speaker_notes": content.speaker_notes,
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def fingerprint_page(content: NormalizedPageContent) -> PageFingerprint:
    canonical = canonical_fingerprint_input(content)
    return PageFingerprint(
        version=FINGERPRINT_VERSION,
        sha256=hashlib.sha256(canonical).hexdigest(),
    )


def _serialize_table(table: NormalizedTable) -> dict[str, Any]:
    grid: list[list[dict[str, Any]]] = []
    for row in table.grid:
        serialized_row: list[dict[str, Any]] = []
        for slot in row:
            if slot.kind == "origin":
                if slot.cell is None:
                    raise ValueError("规范表格的 origin 单元格缺少内容")
                serialized_row.append(
                    {
                        "kind": "origin",
                        "text": slot.cell.text,
                        "col_span": slot.cell.col_span,
                        "row_span": slot.cell.row_span,
                    }
                )
            else:
                serialized_row.append(
                    {
                        "kind": "covered",
                        "origin_row": slot.origin_row,
                        "origin_col": slot.origin_col,
                    }
                )
        grid.append(serialized_row)
    return {"kind": table.kind, "header_rows": table.header_rows, "grid": grid}
