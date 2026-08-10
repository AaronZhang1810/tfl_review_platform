"""Unit tests for the deterministic STRUCTURAL checks + finding signature/dedupe.

After the AI-review inversion, checks.py produces only the three structural findings
(blank pages 1.1 / FMT-010, missing-vs-prior 1.2 / XOUT-020, numbering gap 1.3 /
XOUT-001). All numeric / version / pooled judging moved to the AI judges in
ai_review.py and is covered by test_ai_review.py.
"""

import checks


# --- 1.3 / XOUT-001: numbering gap ----------------------------------------- #

def test_toc_gap_flags_real_gap():
    # 4.4 missing between 4.3 and 4.5, and no 4.4.x exists.
    nums = ["4.1", "4.2", "4.3", "4.5", "4.6"]
    f = checks.toc_gap_findings(nums)
    assert len(f) == 1
    assert f[0]["check_id"] == "XOUT-001"
    assert "Table 4.4" in f[0]["message"]


def test_toc_gap_no_false_positive_for_subnumbered():
    # 2.2 is "present" via 2.2.1..2.2.2 — must NOT be flagged as missing.
    nums = ["1", "2.1", "2.2.1", "2.2.2", "2.3", "3.1"]
    assert checks.toc_gap_findings(nums) == []


# --- 1.2 / XOUT-020: missing output vs prior ------------------------------- #

def test_missing_output_vs_prior_flags_dropped_output():
    fs = checks.missing_output_findings(
        ["Table 1", "Table 2.1"],
        ["Table 1", "Table 2.1", "Table 14.3.1"])
    assert len(fs) == 1
    assert fs[0]["check_id"] == "XOUT-020"
    assert fs[0]["affected"] == ["Table 14.3.1"]


def test_missing_output_normalizes_case_and_spacing():
    # Labels differing only by case/whitespace are the SAME output → not missing.
    assert checks.missing_output_findings(["table 1", "TABLE  2.1"],
                                          ["Table 1", "Table 2.1"]) == []


# --- 1.1 / FMT-010: blank pages (char-count backend monkeypatched) --------- #

def test_blank_page_findings_attributes_to_owning_output(monkeypatch):
    # page 2 is empty; it falls inside Table 1's range (pages 1-2).
    monkeypatch.setattr(checks.pdftools, "page_char_counts", lambda p: [500, 0, 400])
    outs = [{"label": "Table 1", "page_start": 1, "page_end": 2},
            {"label": "Table 2", "page_start": 3, "page_end": 3}]
    fs = checks.blank_page_findings("ignored.pdf", outs)
    assert len(fs) == 1
    assert fs[0]["check_id"] == "FMT-010"
    assert fs[0]["page"] == 2
    assert fs[0]["_output_label"] == "Table 1"


# --- finding signature (stable identity for the human-review log) ---------- #

def test_finding_signature_is_order_and_family_stable():
    a = checks.finding_signature("AIW-2.1", "Table 1", [12, 5.0], "Sum mismatch: 12 != 5")
    b = checks.finding_signature("AIW-2.1", "Table 1", [5, 12], "Sum mismatch: 12 != 5")
    # cited-number order is irrelevant (they are sorted) → identical signature
    assert a == b
    # the leading token is the check FAMILY (prefix before the dash)
    assert a.startswith("AIW|")


def test_finding_signature_differs_on_output_and_message():
    base = checks.finding_signature("AIW-2.1", "Table 1", [1, 2], "msg one")
    assert base != checks.finding_signature("AIW-2.1", "Table 2", [1, 2], "msg one")
    assert base != checks.finding_signature("AIW-2.1", "Table 1", [1, 2], "msg two")


# --- dedupe ---------------------------------------------------------------- #

def test_dedupe_collapses_same_family_same_cell():
    # Two judge findings of the SAME family on the SAME cell (page/numbers/message)
    # collapse to one; a distinct cell survives.
    dup = [
        {"check_id": "AIW-2.1", "page": 1, "numbers": [1, 2], "message": "Row X: 1 != 2"},
        {"check_id": "AIW-2.3", "page": 1, "numbers": [1, 2], "message": "Row X: 1 != 2"},
        {"check_id": "AIW-2.1", "page": 2, "numbers": [3, 4], "message": "Row Y"},
    ]
    assert len(checks.dedupe(dup)) == 2
