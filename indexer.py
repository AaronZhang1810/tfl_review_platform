"""Build the list of outputs (tables/listings/figures) for a delivery PDF.

Two sources, in priority order:
  1. The PDF's bookmark outline — each *leaf* bookmark is one output; its page range
     runs from its destination page up to the next leaf's page. Section-header
     bookmarks that merely *contain* other tables are skipped (they are not outputs).
  2. A fallback header scan — if a PDF has no usable outline, detect "TABLE x",
     "LISTING x", "FIGURE x" lines at the top of pages and start a new output
     whenever the label changes.

Each output gets: output_type, number, label ("Table 4.2.1"), title, page range.
"""

from __future__ import annotations

import logging
import re

import pypdf

from pdftools import page_top_texts

logger = logging.getLogger("tlf.index")

# "TABLE 4.2.1   Some title...", "Listing 16.1", "Figure 2" — the keyword may be
# preceded by leading noise (search, not match). The number is the full separated
# string: SAS-generated captions use a double underscore between the table series
# and the table number ("TABLE 14.1__6.3"), so `.` and `_` are both separators and
# the whole run (14.1__6.3, not 14.1 or 14) is the number. No trailing `\b` — it
# fails between a digit and an underscore and would truncate 14.1__6.3 back to 14.
_SEP_NUM = r"[0-9]+(?:[._]+[0-9]+)*"
_LABEL_RE = re.compile(
    r"\b(TABLE|LISTING|FIGURE)\s+(" + _SEP_NUM + r")\s*(.*)$", re.IGNORECASE
)
# Keyword-less bookmark titles like "14.1.6  Summary of AEs" / "14.1__6.3 …" — a
# *separated* number (at least one `.`/`_` group) so a stray leading integer in
# prose isn't mistaken for one.
_NUM_ONLY_RE = re.compile(r"^\s*([0-9]+(?:[._]+[0-9]+)+)\s*(.*)$")


def _title_case(kind: str) -> str:
    return {"TABLE": "Table", "LISTING": "Listing", "FIGURE": "Figure"}.get(kind.upper(), kind.title())


def _parse_label(text: str):
    s = (text or "").strip()
    m = _LABEL_RE.search(s)
    if m:
        kind, number, rest = m.group(1), m.group(2), m.group(3).strip(" -–\t")
        return _title_case(kind), number, rest
    m = _NUM_ONLY_RE.match(s)  # keyword-less dotted number → assume a Table
    if m:
        return "Table", m.group(1), m.group(2).strip(" -–\t")
    return None


def _page_caption(text: str):
    """Parse the printed output caption at the top of a page.

    The number/title *printed on the page* ("Table 14.1.6  Summary of …") is the
    ground truth for what an output is — unlike a bookmark title, which can be
    wrong, section-level, or absent. Scans the top lines where a caption sits (a
    few header lines — confidentiality, protocol id, "Page x of y" — may precede
    it). SAS-generated captions print the number on its own line ("TABLE 14.1__6.3")
    with the descriptive title on the following line, so when the caption line has no
    trailing text we take the next non-empty line as the title. Returns
    (output_type, number, title) or None.
    """
    lines = (text or "").splitlines()[:12]
    for i, line in enumerate(lines):
        parsed = _parse_label(line)
        if parsed:
            otype, number, rest = parsed
            if not rest:
                for nxt in lines[i + 1:]:
                    if nxt.strip():
                        rest = nxt.strip()
                        break
            return otype, number, rest
    return None


def _item_title(item) -> str:
    return str(getattr(item, "title", "") or "").strip()


def _resolve_page(reader, item) -> int | None:
    """Best-effort 0-based page index for an outline item, with a fallback for
    named destinations that ``get_destination_page_number`` can't resolve directly."""
    try:
        pg = reader.get_destination_page_number(item)
        if isinstance(pg, int) and pg >= 0:
            return pg
    except Exception:
        pass
    try:
        dest = item.get("/Dest")
    except Exception:
        dest = None
    if isinstance(dest, (str, bytes)):
        key = dest.decode("latin-1", "ignore") if isinstance(dest, bytes) else dest
        try:
            nd = reader.named_destinations.get(key)
            if nd is not None:
                pg = reader.get_destination_page_number(nd)
                if isinstance(pg, int) and pg >= 0:
                    return pg
        except Exception:
            pass
    return None


def _collect_outputs(items) -> list:
    """Return the outline bookmark items that represent real outputs.

    A pypdf outline lists a bookmark's children as a ``list`` immediately following
    the parent item. A bookmark is a *section container* (skipped) when it has
    children AND at least one child parses to a numbered table/listing/figure label.
    A bookmark whose children are only page-navigation markers ("Page 2 of 10",
    which don't parse) is kept as a leaf and its children are ignored.
    """
    out: list = []
    n = len(items)
    for i, item in enumerate(items):
        if isinstance(item, list):
            continue  # a child-block; handled via its parent
        children = items[i + 1] if (i + 1 < n and isinstance(items[i + 1], list)) else None
        if children is not None:
            child_bms = [c for c in children if not isinstance(c, list)]
            if any(_parse_label(_item_title(c)) for c in child_bms):
                out.extend(_collect_outputs(children))  # section: descend, drop the container
                continue
        out.append(item)  # leaf, or a table whose only children are page-nav markers
    return out


def from_bookmarks(path: str) -> list[dict]:
    reader = pypdf.PdfReader(path)
    n = len(reader.pages)
    try:
        items = list(reader.outline)
    except Exception:
        return []

    bookmarks = _collect_outputs(items)
    placed: list[tuple[str, int]] = []
    for it in bookmarks:
        pg = _resolve_page(reader, it)
        if pg is not None:
            placed.append((_item_title(it), pg))
    dropped = len(bookmarks) - len(placed)
    if dropped:
        logger.warning("indexer: %d bookmark(s) had unresolvable destinations; skipped", dropped)
    if not placed:
        return []

    placed.sort(key=lambda t: t[1])
    outputs: list[dict] = []
    for i, (title_text, pg) in enumerate(placed):
        next_pg = placed[i + 1][1] if i + 1 < len(placed) else n
        parsed = _parse_label(title_text)
        if parsed:
            otype, number, rest = parsed
            label = f"{otype} {number}"
            title = rest.strip(" -–\t")
        else:
            otype, number, label, title = "Table", "", title_text, title_text
        page_start = pg + 1
        if i == 0 and pg <= 1:
            page_start = 1  # never drop an off-by-one first page (front matter stays out)
        page_end = max(page_start, next_pg)
        # Collapse an exact duplicate (same start page + same label).
        if outputs and outputs[-1]["label"] == label and outputs[-1]["page_start"] == page_start:
            continue
        outputs.append({
            "seq": len(outputs), "output_type": otype, "number": number,
            "label": label, "title": title,
            "page_start": page_start, "page_end": page_end,
        })
    return outputs


def from_header_scan(path: str) -> list[dict]:
    """Fallback: start a new output whenever the top-of-page label changes."""
    reader = pypdf.PdfReader(path)
    n = len(reader.pages)
    texts = page_top_texts(path)
    outputs: list[dict] = []
    cur = None
    for idx, text in enumerate(texts):
        parsed = _page_caption(text)
        if parsed:
            otype, number, rest = parsed
            label = f"{otype} {number}"
            if cur is None or cur["label"] != label:
                if cur:
                    cur["page_end"] = idx  # previous ended on the prior page
                    outputs.append(cur)
                cur = {
                    "seq": len(outputs), "output_type": otype, "number": number,
                    "label": label, "title": rest.strip(" -–\t"),
                    "page_start": idx + 1, "page_end": idx + 1,
                }
    if cur:
        cur["page_end"] = n
        outputs.append(cur)
    return outputs


def _reconcile_with_captions(path: str, outputs: list[dict]) -> list[dict]:
    """Correct bookmark-derived outputs against the printed page captions.

    The bookmark outline can mislabel a table (its title is wrong or section-level)
    or omit one entirely, which makes the indexer absorb a real table (e.g. 14.1.6)
    into a neighbour and relabel it (e.g. 14.2) — swallowing its first page. The
    printed caption is authoritative, so within each output's page range we:
      * 0 captions   → leave the output unchanged (never drop; protects listings /
                       figures whose captions we can't parse);
      * 1 caption    → relabel to the printed number when it disagrees (the bookmark
                       was wrong); an agreeing caption leaves the output untouched;
      * ≥2 captions  → split into one output per caption at its page boundary (the
                       bookmark had merged several real outputs), recovering both the
                       lost labels and the swallowed first pages.
    If the document has no parseable captions at all (e.g. a scanned/image PDF), the
    bookmark result is returned untouched.
    """
    if not outputs:
        return outputs
    caps = [_page_caption(t) for t in page_top_texts(path)]   # caps[p-1] → page p (fast, C-backed)
    if not any(caps):
        return outputs   # nothing to reconcile against (no extractable captions)

    def cap_at(p):
        return caps[p - 1] if 1 <= p <= len(caps) else None

    rebuilt: list[dict] = []
    for o in outputs:
        ps, pe = o["page_start"], o["page_end"]
        # First page of each DISTINCT caption inside this output's range
        # (continuation pages repeat the caption and don't create a new anchor).
        anchors: list[tuple] = []
        prev_key = None
        for p in range(ps, pe + 1):
            c = cap_at(p)
            if not c:
                continue
            key = (c[0].lower(), c[1])
            if key != prev_key:
                anchors.append((p, c))
                prev_key = key

        if not anchors:
            rebuilt.append(dict(o))
            continue

        if len(anchors) == 1:
            _, (otype, number, rest) = anchors[0]
            if number and number != o.get("number"):
                fixed = dict(o)
                fixed.update(output_type=otype, number=number, label=f"{otype} {number}")
                if rest:
                    fixed["title"] = rest
                rebuilt.append(fixed)
            else:
                rebuilt.append(dict(o))   # caption confirms the bookmark → untouched
            continue

        # ≥2 captions merged under one bookmark: split at caption boundaries.
        for j, (p0, (otype, number, rest)) in enumerate(anchors):
            seg_start = ps if j == 0 else p0     # keep the original start for the head segment
            seg_end = anchors[j + 1][0] - 1 if j + 1 < len(anchors) else pe
            rebuilt.append({
                "seq": 0, "output_type": otype, "number": number,
                "label": f"{otype} {number}", "title": rest,
                "page_start": seg_start, "page_end": max(seg_start, seg_end),
            })

    # Merge a run of same-label segments into one contiguous output. One real table
    # whose continuation pages straddle a bookmark boundary is split by the per-range
    # loop above (e.g. "14.1__4" on p4 and p5 landing in adjacent bookmark ranges);
    # since the printed number is unique per output, adjacent/overlapping same-label
    # segments are the same table and are stitched back into a single page range.
    merged: list[dict] = []
    for o in rebuilt:
        if merged and merged[-1]["label"] == o["label"] and o["page_start"] <= merged[-1]["page_end"] + 1:
            merged[-1]["page_end"] = max(merged[-1]["page_end"], o["page_end"])
            continue
        merged.append(o)
    for k, o in enumerate(merged):
        o["seq"] = k
    return merged


def index_delivery(path: str) -> list[dict]:
    outputs = from_bookmarks(path)
    if outputs:
        # Bookmark titles can be wrong / section-level / missing; the printed page
        # captions are ground truth, so reconcile the bookmark result against them.
        return _reconcile_with_captions(path, outputs)
    # No usable outline: the header scan is already caption-derived.
    return from_header_scan(path)
