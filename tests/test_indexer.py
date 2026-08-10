"""Tests for caption reconciliation — the printed page caption is ground truth.

The bookmark outline can mislabel a table, merge several under one bookmark, or omit
one entirely (absorbing a real table like 14.1.6 into a neighbour "14.2" and swallowing
its first page). `index_delivery` reconciles the bookmark result against the caption
actually printed at the top of each page. These tests build real text PDFs (fpdf2) so
pdfplumber can read the captions, then attach a deliberately-wrong bookmark outline
(pypdf) and assert the reconciliation fixes it.

`from_bookmarks` on blank (textless) PDFs is covered separately in test_indexer_safety;
here every synthetic page carries printed text so the caption pass is exercised.
"""

import io

import fpdf
import pypdf

import indexer


def _pdf(tmp_path, page_texts, build_outline):
    """An N-page PDF whose page i prints ``page_texts[i]`` (None = blank) at the top,
    with a bookmark outline added by ``build_outline(writer)``."""
    doc = fpdf.FPDF()
    doc.set_font("Helvetica", size=12)
    for txt in page_texts:
        doc.add_page()
        if txt:
            doc.set_xy(10, 10)
            doc.cell(0, 10, txt)
    reader = pypdf.PdfReader(io.BytesIO(bytes(doc.output())))
    w = pypdf.PdfWriter()
    for pg in reader.pages:
        w.add_page(pg)
    build_outline(w)
    path = tmp_path / "delivery.pdf"
    buf = io.BytesIO()
    w.write(buf)
    path.write_bytes(buf.getvalue())
    return str(path)


def _tuples(outs):
    return [(o["label"], o["number"], o["page_start"], o["page_end"]) for o in outs]


def test_split_merged_bookmark_recovers_label_and_first_page(tmp_path):
    """The reported bug: 14.1.6 has no bookmark, so its pages are absorbed into the
    neighbouring "14.2" bookmark — relabelling it 14.2 and hiding its first page. Two
    distinct captions in one bookmark range → split into two outputs, each with the
    printed label and its own first page restored."""
    pages = ["TABLE 14.1.6  Summary of AEs by SOC", None, "TABLE 14.2  Deaths", None]
    p = _pdf(tmp_path, pages, lambda w: w.add_outline_item("TABLE 14.2  wrong", 0))

    # The raw bookmark pass mislabels everything as one 14.2 spanning all four pages.
    assert _tuples(indexer.from_bookmarks(p)) == [("Table 14.2", "14.2", 1, 4)]
    # Reconciliation splits it and restores 14.1.6 with page 1.
    outs = indexer.index_delivery(p)
    assert _tuples(outs) == [
        ("Table 14.1.6", "14.1.6", 1, 2),
        ("Table 14.2", "14.2", 3, 4),
    ]
    assert outs[0]["page_start"] == 1              # swallowed first page recovered
    assert [o["seq"] for o in outs] == [0, 1]      # re-sequenced


def test_relabel_single_caption_overrides_wrong_bookmark(tmp_path):
    """One table, one caption, but the bookmark number is wrong → the printed caption
    wins (relabel) while the page range is left intact."""
    pages = ["TABLE 14.1.6  Real caption", None]
    p = _pdf(tmp_path, pages, lambda w: w.add_outline_item("TABLE 14.2  bogus", 0))

    assert _tuples(indexer.from_bookmarks(p)) == [("Table 14.2", "14.2", 1, 2)]
    outs = indexer.index_delivery(p)
    assert _tuples(outs) == [("Table 14.1.6", "14.1.6", 1, 2)]
    assert outs[0]["title"] == "Real caption"      # title taken from the printed caption


def test_caption_agrees_preserves_bookmark_output(tmp_path):
    """When the printed number matches the bookmark, the output is left untouched —
    the bookmark's title is kept even if the caption's trailing text differs. This is
    the annual-edition shape (one clean caption per bookmark) and must never be perturbed."""
    def build(w):
        w.add_outline_item("TABLE 1  Bookmark Title For Exposure", 0)
        w.add_outline_item("TABLE 2  Bookmark Title For Demographics", 2)
    pages = ["TABLE 1  Printed Exposure", None, "TABLE 2  Printed Demographics", None]
    p = _pdf(tmp_path, pages, build)

    before = indexer.from_bookmarks(p)
    after = indexer.index_delivery(p)
    assert _tuples(after) == _tuples(before)       # numbers/pages unchanged
    # Titles come from the bookmark (not clobbered by the agreeing caption).
    assert [o["title"] for o in after] == [
        "Bookmark Title For Exposure", "Bookmark Title For Demographics"]


def test_caption_absent_output_left_untouched(tmp_path):
    """An output whose pages carry no parseable caption (e.g. a figure that is an
    image) is left exactly as the bookmark had it, even while a sibling table in the
    same document is reconciled. Never drop what we can't read."""
    def build(w):
        w.add_outline_item("TABLE 14.2  wrong", 0)     # pages 1-2, caption says 14.1.6
        w.add_outline_item("FIGURE 2  Overall Survival", 2)   # page 3, no caption text
    pages = ["TABLE 14.1.6  Summary", None, "Kaplan-Meier plot of overall survival"]
    p = _pdf(tmp_path, pages, build)

    outs = indexer.index_delivery(p)
    assert _tuples(outs) == [
        ("Table 14.1.6", "14.1.6", 1, 2),          # sibling table relabelled
        ("Figure 2", "2", 3, 3),                   # caption-less figure kept as-is
    ]


def test_double_underscore_number_is_parsed_whole():
    """SAS-generated captions separate the table series from the table number with a
    double underscore ("TABLE 14.1__6.3"). The whole run is the number — the reported
    bug was the parser truncating it to "14" (so 45 tables collapsed to "Table 14")."""
    otype, number, rest = indexer._parse_label("TABLE 14.1__6.3  Pre-Treatment Anesthesia")
    assert (otype, number, rest) == ("Table", "14.1__6.3", "Pre-Treatment Anesthesia")
    # Keyword-less form (bookmark titles) parses the same way.
    assert indexer._parse_label("14.2__3.9.1  Facial Laxity")[:2] == ("Table", "14.2__3.9.1")


def test_caption_title_taken_from_following_line():
    """When the number prints on its own line (SAS style), the title on the next line
    is used — not left blank."""
    text = "STUDY SYN-A101\nTABLE PAGE 1 OF 1\nTABLE 14.1__1\nSubject Disposition\n(Screened Subjects)"
    assert indexer._page_caption(text) == ("Table", "14.1__1", "Subject Disposition")


def test_continuation_across_bookmark_boundary_merges(tmp_path):
    """One real table (14.1__4) spanning several pages, but resolved bookmarks split
    its range — the per-range reconcile makes two same-label segments, which must be
    stitched back into one contiguous output (not shown as duplicate rows)."""
    pages = ["TABLE 14.1__4  Demographics"] * 3
    def build(w):
        w.add_outline_item("TABLE 14.1__4  x", 0)
        w.add_outline_item("TABLE 14.1__4  x", 1)   # boundary inside the same table
    p = _pdf(tmp_path, pages, build)

    # Raw bookmarks split the table across the boundary...
    assert len(indexer.from_bookmarks(p)) == 2
    # ...reconciliation stitches it back into one output covering all three pages.
    assert _tuples(indexer.index_delivery(p)) == [("Table 14.1__4", "14.1__4", 1, 3)]


def test_scanned_pdf_without_text_is_untouched(tmp_path):
    """If no page has extractable caption text (a scanned/image delivery), the
    bookmark result is returned verbatim — reconciliation cannot invent captions and
    must not throw away the only structure we have."""
    def build(w):
        w.add_outline_item("TABLE 1  Alpha", 0)
        w.add_outline_item("TABLE 2  Beta", 2)
    p = _pdf(tmp_path, [None, None, None, None], build)   # all blank pages

    assert _tuples(indexer.index_delivery(p)) == _tuples(indexer.from_bookmarks(p))
    assert [o["label"] for o in indexer.index_delivery(p)] == ["Table 1", "Table 2"]
