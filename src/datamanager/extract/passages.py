"""Passage splitting (FR-7).

Passages are the retrieval floor: even a document no extractor understands is
findable here. Splitting follows document structure -- blank lines, headings,
table rows -- rather than blind fixed windows, so a passage tends to be a
coherent unit rather than a sentence cut in half.

Passages never carry the burden of binding related facts together; that is the
job of records (FR-3), extracted from whole-document text before any splitting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# A heading-ish line: markdown heading, all-caps label, or "Section:" style.
_HEADING = re.compile(r"^\s{0,3}(#{1,6}\s+\S|[A-Z][A-Z0-9 &/'()-]{3,60}:?\s*$|\d+\.\s+\S)")
_BLANK_RUN = re.compile(r"\n\s*\n")


@dataclass
class Passage:
    ordinal: int
    text: str
    page: int | None
    char_start: int
    char_end: int


def split(text: str, *, pages: list[str] | None = None,
          target_chars: int = 1200, overlap_chars: int = 150) -> list[Passage]:
    """Split text into passages, tracking page and character offsets."""
    if not text.strip():
        return []

    if pages and len(pages) > 1:
        return _split_paged(pages, target_chars, overlap_chars)

    blocks = _blocks(text, 0)
    return _pack(blocks, target_chars, overlap_chars, page=None)


def _split_paged(pages: list[str], target_chars: int,
                 overlap_chars: int) -> list[Passage]:
    """Split page by page so every passage keeps a real page number.

    Passages never span a page boundary -- a citation pointing at two pages is
    not a citation.
    """
    out: list[Passage] = []
    offset = 0
    ordinal = 0
    for page_no, page_text in enumerate(pages, start=1):
        if page_text.strip():
            blocks = _blocks(page_text, offset)
            for passage in _pack(blocks, target_chars, overlap_chars, page=page_no):
                passage.ordinal = ordinal
                ordinal += 1
                out.append(passage)
        offset += len(page_text) + 2  # the "\n\n" joiner used when pages were merged
    return out


def _blocks(text: str, base_offset: int) -> list[tuple[str, int, int]]:
    """Break text into (block, start, end) on blank lines, then on headings."""
    raw: list[tuple[str, int, int]] = []
    pos = 0
    for match in _BLANK_RUN.finditer(text):
        chunk = text[pos:match.start()]
        if chunk.strip():
            raw.append((chunk, base_offset + pos, base_offset + match.start()))
        pos = match.end()
    tail = text[pos:]
    if tail.strip():
        raw.append((tail, base_offset + pos, base_offset + len(text)))

    # Split further at headings so a section header starts a passage.
    out: list[tuple[str, int, int]] = []
    for chunk, start, end in raw:
        lines = chunk.splitlines(keepends=True)
        current: list[str] = []
        current_start = start
        cursor = start
        for line in lines:
            if _HEADING.match(line) and current and "".join(current).strip():
                body = "".join(current)
                out.append((body, current_start, cursor))
                current = [line]
                current_start = cursor
            else:
                current.append(line)
            cursor += len(line)
        if current and "".join(current).strip():
            out.append(("".join(current), current_start, end))
    return out


def _pack(blocks: list[tuple[str, int, int]], target_chars: int,
          overlap_chars: int, page: int | None) -> list[Passage]:
    """Group small blocks up to the target size; split oversized ones."""
    passages: list[Passage] = []
    buf: list[str] = []
    buf_start: int | None = None
    buf_end = 0

    def flush() -> None:
        nonlocal buf, buf_start, buf_end
        if buf and buf_start is not None:
            # Join on a newline: concatenating bare blocks fuses the last word
            # of one to the first of the next ("2023Employer").
            body = "\n".join(part.strip("\n") for part in buf).strip()
            if body:
                passages.append(Passage(
                    ordinal=len(passages), text=body,
                    page=page, char_start=buf_start, char_end=buf_end,
                ))
        buf, buf_start = [], None

    for block, start, end in blocks:
        if len(block) > target_chars * 1.5:
            flush()
            passages.extend(_split_long(block, start, target_chars,
                                        overlap_chars, page, len(passages)))
            continue

        if buf_start is not None and (buf_end - buf_start) + len(block) > target_chars:
            flush()
        if buf_start is None:
            buf_start = start
        buf.append(block)
        buf_end = end

    flush()
    for i, passage in enumerate(passages):
        passage.ordinal = i
    return passages


def _split_long(block: str, start: int, target_chars: int, overlap_chars: int,
                page: int | None, ordinal_base: int) -> list[Passage]:
    """Split an oversized block on sentence boundaries, with overlap.

    Overlap exists so a fact sitting on a split boundary is not lost to both
    sides.
    """
    out: list[Passage] = []
    pos = 0
    n = len(block)
    while pos < n:
        end = min(pos + target_chars, n)
        if end < n:
            window = block[pos:end]
            for sep in ("\n", ". ", "; ", ", ", " "):
                cut = window.rfind(sep)
                if cut > target_chars * 0.5:
                    end = pos + cut + len(sep)
                    break
        body = block[pos:end].strip()
        if body:
            out.append(Passage(
                ordinal=ordinal_base + len(out), text=body, page=page,
                char_start=start + pos, char_end=start + end,
            ))
        if end >= n:
            break
        pos = max(end - overlap_chars, pos + 1)
    return out
