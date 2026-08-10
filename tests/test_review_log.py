"""The human-review learning log (db.review_log) — capture only; no rule is derived
from it yet. Verifies that every human decision is recorded with full context, that a
finding's check_id maps back to its checklist item, and that the finding signature is
stable across identical findings.
"""

import json

import pytest

import db
import checks


@pytest.fixture()
def iso_db(tmp_path, monkeypatch):
    """Point db at a throwaway file, reset the thread-local connection, create schema.
    Self-contained (does not use the server-only `temp_db` conftest fixture) so this
    test file is byte-identical across both editions."""
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "app.db"))
    if hasattr(db._local, "conn"):
        del db._local.conn
    db.init()
    yield tmp_path
    if hasattr(db._local, "conn"):
        del db._local.conn


def _seed_output(pid, label="Table 1"):
    # A real document row is required: output.document_id is a FK (PRAGMA foreign_keys=ON).
    did = db.insert("document", project_id=pid, role="delivery", filename="d.pdf",
                    path="d.pdf", n_pages=3, edition="2025")
    return db.insert("output", project_id=pid, document_id=did, seq=0, output_type="Table",
                     number="1", label=label, title="Summary", page_start=1, page_end=3)


def test_log_finding_action_records_full_context(iso_db):
    pid = db.insert("project", compound="C", study="S", name="p", edition_label="",
                    created_at=db.now_iso())
    oid = _seed_output(pid, "Table 2.1")
    fid = db.insert("finding", project_id=pid, output_id=oid, run_id=1,
                    check_id="AIW-2.1", severity="major", risk="High",
                    message="Group-N sum mismatch: 1240+6 != 1250",
                    subjects=json.dumps(["SYN-A102"]), numbers=json.dumps([1240, 6, 1250]),
                    page=3, printed_page=2, pages_total=10, section="Condition Alpha",
                    row_kind="aggregate", signature="AIW|table 2 1|(6.0, 1240.0, 1250.0)|x",
                    state="pending", badge="", affected=json.dumps([]))
    f = db.one("SELECT * FROM finding WHERE id=?", (fid,))

    db.log_finding_action("Alice", "reject", f, comment_text="expected — pooled separately")

    row = db.one("SELECT * FROM review_log ORDER BY id DESC LIMIT 1")
    assert row["reviewer"] == "Alice"
    assert row["action"] == "reject"
    assert row["project_id"] == pid
    assert row["output_label"] == "Table 2.1"          # resolved from output_id
    assert row["check_id"] == "AIW-2.1"
    assert row["checklist_item"] == "2.1"              # prefix stripped
    assert row["risk"] == "High"
    assert row["numbers"] == json.dumps([1240, 6, 1250])
    assert row["subjects"] == json.dumps(["SYN-A102"])
    assert row["page"] == 3 and row["printed_page"] == 2 and row["section"] == "Condition Alpha"
    assert row["comment_text"] == "expected — pooled separately"
    assert row["finding_signature"] == "AIW|table 2 1|(6.0, 1240.0, 1250.0)|x"


def test_log_finding_action_resolves_label_from_affected(iso_db):
    # A cross-output finding has no owning output_id; the label comes from affected[0].
    pid = db.insert("project", compound="C", study="S", name="p", edition_label="",
                    created_at=db.now_iso())
    fid = db.insert("finding", project_id=pid, output_id=None, run_id=1,
                    check_id="AIX-3", severity="major", risk="High", message="pooled mismatch",
                    subjects="[]", numbers="[]", affected=json.dumps(["Table 2.1", "Table 1"]))
    f = db.one("SELECT * FROM finding WHERE id=?", (fid,))
    db.log_finding_action("Bob", "post", f, comment_text="")
    row = db.one("SELECT * FROM review_log ORDER BY id DESC LIMIT 1")
    assert row["output_label"] == "Table 2.1"
    assert row["checklist_item"] == "3"


@pytest.mark.parametrize("check_id, item", [
    ("AIW-2.1", "2.1"), ("AIX-3", "3"), ("AIV-6.2", "6.2"),
    ("FMT-010", "1.1"), ("XOUT-020", "1.2"), ("XOUT-001", "1.3"),
    ("FMT-002", "2"), ("NOPE-9", ""), ("", ""),
])
def test_checklist_item_for_mapping(check_id, item):
    assert db.checklist_item_for(check_id) == item


def test_log_comment_action_records_manual_review(iso_db):
    pid = db.insert("project", compound="C", study="S", name="p", edition_label="",
                    created_at=db.now_iso())
    db.log_comment_action("Carol", "comment_resolve", pid, "Table 5.1", "looks fine now")
    row = db.one("SELECT * FROM review_log ORDER BY id DESC LIMIT 1")
    assert row["reviewer"] == "Carol"
    assert row["action"] == "comment_resolve"
    assert row["output_label"] == "Table 5.1"
    assert row["comment_text"] == "looks fine now"
    assert row["check_id"] == "" and row["checklist_item"] == ""


def test_finding_signature_matches_stored_key_across_identical_findings(iso_db):
    # Two runs producing the same finding get the same signature — the key a future
    # step will use to correlate a finding with the human-review log.
    s1 = checks.finding_signature("AIW-2.1", "Table 1", [12, 5], "Sum mismatch: 12 != 5")
    s2 = checks.finding_signature("AIW-2.1", "Table 1", [5, 12], "Sum mismatch: 12 != 5")
    assert s1 == s2 and s1
