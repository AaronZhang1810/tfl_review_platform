"""Tests for output-label parsing, bookmark indexing, and filename safety."""

import io

import pypdf

import indexer
import main


def test_parse_label_variants():
    assert indexer._parse_label("TABLE 4.2.1   MM Pooled Studies - Overview") == (
        "Table", "4.2.1", "MM Pooled Studies - Overview")
    assert indexer._parse_label("Listing 16.1 Adverse Events")[:2] == ("Listing", "16.1")
    assert indexer._parse_label("FIGURE 2 Kaplan-Meier")[:2] == ("Figure", "2")
    assert indexer._parse_label("Not a table heading") is None


def test_parse_label_keywordless_and_noise():
    # Bookmark title with no TABLE/LISTING/FIGURE keyword — a dotted number → Table.
    assert indexer._parse_label("14.1.6  Summary of AEs by SOC") == (
        "Table", "14.1.6", "Summary of AEs by SOC")
    # Leading noise before the keyword is tolerated.
    assert indexer._parse_label("Appendix: Table 14.1.6 Foo")[:2] == ("Table", "14.1.6")
    # A bare integer with no dot and no keyword is NOT a table (avoids false hits).
    assert indexer._parse_label("5 mg cohort summary") is None
    # Full dotted number is captured, never truncated to the first level.
    assert indexer._parse_label("TABLE 14.1.6 x")[1] == "14.1.6"


# --------------------------------------------------------------------------- #
# from_bookmarks: nested outlines, page-nav children, first-page, flat regress
# --------------------------------------------------------------------------- #

def _write_pdf(tmp_path, n_pages, build_outline):
    """Create an n_pages blank PDF; build_outline(writer) adds bookmarks."""
    w = pypdf.PdfWriter()
    for _ in range(n_pages):
        w.add_blank_page(width=200, height=200)
    build_outline(w)
    path = tmp_path / "delivery.pdf"
    buf = io.BytesIO()
    w.write(buf)
    path.write_bytes(buf.getvalue())
    return str(path)


def test_bookmarks_skip_section_containers(tmp_path):
    """A section container (whose children are numbered tables) is not an output;
    only the leaf tables are, with contiguous page ranges and no lost first page."""
    def build(w):
        sec = w.add_outline_item("TABLE 14.1  Section container", 1)
        w.add_outline_item("TABLE 14.1.1  First child", 1, parent=sec)
        w.add_outline_item("TABLE 14.1.6  Sixth child", 4, parent=sec)
        w.add_outline_item("14.2  Keyword-less next", 7)   # keyword-less dotted title

    outs = indexer.from_bookmarks(_write_pdf(tmp_path, 10, build))
    labels = [(o["label"], o["page_start"], o["page_end"]) for o in outs]
    assert labels == [
        ("Table 14.1.1", 1, 4),   # container dropped; first page (1) kept
        ("Table 14.1.6", 5, 7),
        ("Table 14.2", 8, 10),    # keyword-less title parsed as a Table
    ]
    assert [o["number"] for o in outs] == ["14.1.1", "14.1.6", "14.2"]
    assert [o["seq"] for o in outs] == [0, 1, 2]


def test_bookmarks_keep_table_with_pagenav_children(tmp_path):
    """A real table whose only children are page-nav markers ('Page 2 of 3',
    which don't parse) is kept as an output; the nav children are ignored."""
    def build(w):
        t = w.add_outline_item("TABLE 3.1  Big table", 0)
        w.add_outline_item("Page 2 of 3", 1, parent=t)
        w.add_outline_item("Page 3 of 3", 2, parent=t)
        w.add_outline_item("TABLE 3.2  Next", 3)

    outs = indexer.from_bookmarks(_write_pdf(tmp_path, 6, build))
    assert [(o["label"], o["page_start"], o["page_end"]) for o in outs] == [
        ("Table 3.1", 1, 3),
        ("Table 3.2", 4, 6),
    ]


def test_bookmarks_first_page_not_lost(tmp_path):
    """First bookmark at 0-based index 1 must still cover page 1 (off-by-one fix)."""
    def build(w):
        w.add_outline_item("TABLE 1  Alpha", 1)   # 0-based 1 -> would start at page 2
        w.add_outline_item("TABLE 2  Beta", 3)

    outs = indexer.from_bookmarks(_write_pdf(tmp_path, 6, build))
    assert outs[0]["page_start"] == 1
    assert [(o["label"], o["page_start"], o["page_end"]) for o in outs] == [
        ("Table 1", 1, 3), ("Table 2", 4, 6)]


def test_bookmarks_flat_unchanged(tmp_path):
    """A flat outline (the IB-sample shape) emits every bookmark, ranges intact."""
    def build(w):
        w.add_outline_item("TABLE 1  Exposure", 0)
        w.add_outline_item("TABLE 1.1  Listing", 2)
        w.add_outline_item("LISTING 16.1  Subjects", 4)

    outs = indexer.from_bookmarks(_write_pdf(tmp_path, 6, build))
    assert [(o["output_type"], o["number"], o["page_start"], o["page_end"]) for o in outs] == [
        ("Table", "1", 1, 2),
        ("Table", "1.1", 3, 4),
        ("Listing", "16.1", 5, 6),
    ]


def test_safe_filename_blocks_traversal():
    assert main.safe_filename("../../etc/passwd") == "etc_passwd" or \
           main.safe_filename("../../etc/passwd") == "passwd"
    assert "/" not in main.safe_filename("a/b/c.pdf")
    assert "\\" not in main.safe_filename(r"..\..\win.pdf")
    # keeps a reasonable name
    assert main.safe_filename("IB_TABLE_IB18.pdf") == "IB_TABLE_IB18.pdf"
    # never empty
    assert main.safe_filename("") == "upload"
    assert main.safe_filename("...") == "upload"


def test_safe_filename_basename_only():
    # The final component is preserved; parent dirs are stripped.
    out = main.safe_filename(r"C:\accounts\x\..\..\secret.pdf")
    assert out == "secret.pdf"
