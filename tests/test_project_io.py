"""Round-trip tests for project export / import (project_io) — LOCAL edition.

Seeds a project with the awkward shapes that break a naive id-remap (a cross-output XOUT finding with output_id=None, a comment reply, an AI comment tied to a finding, an annotation, and an audit row whose target row was later deleted), then exports and re-imports it and checks that every foreign key and non-FK logical reference was remapped to the NEW project's ids — plus the hostile-bundle, missing-PDF, atomicity, ask/replace edge cases."""

from __future__ import annotations

import io
import json
import os
import zipfile

import pytest
from pypdf import PdfWriter

import db
import project_io


@pytest.fixture()
def iso_db(tmp_path, monkeypatch):
    """Throwaway DB + reset thread-local connection (mirrors test_review_log)."""
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "app.db"))
    if hasattr(db._local, "conn"):
        del db._local.conn
    db.init()
    yield tmp_path
    if hasattr(db._local, "conn"):
        del db._local.conn


@pytest.fixture()
def safe_name():
    """The real path-sanitiser the endpoint injects into import_bundle."""
    import main
    return main.safe_filename


def _pdf_bytes(n_pages: int = 1) -> bytes:
    w = PdfWriter()
    for _ in range(n_pages):
        w.add_blank_page(width=200, height=200)
    b = io.BytesIO()
    w.write(b)
    return b.getvalue()


def _seed_project(uploads_dir: str) -> dict:
    """Create a rich project with tricky shapes. Returns a dict of the ids/paths."""
    pid = db.insert("project", compound="Compound Z", study="SYN-1", name="Annual 2025",
                    edition_label="Annual edition (2025)", created_at=db.now_iso())
    pdir = os.path.join(uploads_dir, str(pid))
    os.makedirs(pdir, exist_ok=True)

    p1 = os.path.join(pdir, "delivery.pdf")
    with open(p1, "wb") as fh:
        fh.write(_pdf_bytes(3))
    d1 = db.insert("document", project_id=pid, role="delivery", filename="delivery.pdf",
                   path=p1, n_pages=3, edition="2025")
    p2 = os.path.join(pdir, "sap.pdf")
    with open(p2, "wb") as fh:
        fh.write(_pdf_bytes(1))
    d2 = db.insert("document", project_id=pid, role="sap", filename="sap.pdf",
                   path=p2, n_pages=1, edition="")

    o1 = db.insert("output", project_id=pid, document_id=d1, seq=0, output_type="Table",
                   number="1", label="Table 1", title="Demographics", page_start=1, page_end=2,
                   status="Approved", extraction_json='{"rows":[{"n":1240}]}',
                   content_hash="hash-abc")
    o2 = db.insert("output", project_id=pid, document_id=d1, seq=1, output_type="Table",
                   number="2", label="Table 2", title="AEs", page_start=3, page_end=3,
                   status="In Progress", extraction_json='{"rows":[]}', content_hash="hash-def")

    run = db.insert("ai_run", project_id=pid, kind="fresh", started_at=db.now_iso(),
                    finished_at=db.now_iso(), summary_json='{"findings":2}')

    f1 = db.insert("finding", project_id=pid, output_id=o1, run_id=run, check_id="AIW-2.1",
                   severity="major", message="sum mismatch", risk="High",
                   subjects=json.dumps(["SYN-A102"]), numbers=json.dumps([1240, 6, 1250]),
                   page=2, printed_page=1, pages_total=10, section="Condition Alpha",
                   row_kind="aggregate", signature="sig-1", state="pending", badge="",
                   affected=json.dumps([]))
    # Cross-output finding: no owning output (output_id=None).
    f2 = db.insert("finding", project_id=pid, output_id=None, run_id=run, check_id="XOUT-001",
                   severity="minor", message="numbering gap", risk="Low",
                   subjects="[]", numbers="[]", signature="sig-2", state="pending", badge="",
                   affected=json.dumps(["Table 1", "Table 2"]))

    c1 = db.insert("comment", project_id=pid, output_id=o1, title="Q", body="please check",
                   source="manual", author="Alice", parent_id=None, created_at=db.now_iso())
    c2 = db.insert("comment", project_id=pid, output_id=o1, title="", body="agreed",
                   source="reply", author="Bob", parent_id=c1, created_at=db.now_iso())
    c3 = db.insert("comment", project_id=pid, output_id=o1, title="AIW-2.1",
                   body="AI: sum mismatch", source="ai", finding_id=f1, author="AI",
                   parent_id=None, created_at=db.now_iso())
    # AI comment on a cross-output finding: output_id=None, finding_id set.
    c4 = db.insert("comment", project_id=pid, output_id=None, title="XOUT-001", body="AI: gap",
                   source="ai", finding_id=f2, author="AI", parent_id=None, created_at=db.now_iso())

    a1 = db.insert("annotation", output_id=o1, kind="highlight", page=1,
                   geom_json='{"x":0.1,"y":0.2,"w":0.3,"h":0.4}', note="look here",
                   created_at=db.now_iso())

    db.audit("system", "project.create", "project", pid, pid, "created")
    db.audit("Alice", "status.set", "output", o1, pid, "Not Reviewed -> Approved")
    db.audit("AI", "finding.post", "finding", f1, pid, "AIW-2.1")
    db.audit("Alice", "comment.add", "comment", c1, pid, "Q: please check")
    # A dangling audit target: audit a comment, then delete the comment. Its audit_log row (no FK) survives and must import with entity_id -> NULL.
    tmp_c = db.insert("comment", project_id=pid, output_id=o1, title="tmp", body="tmp",
                      source="manual", author="Alice", parent_id=None, created_at=db.now_iso())
    db.audit("Alice", "comment.add", "comment", tmp_c, pid, "temp")
    db.execute("DELETE FROM comment WHERE id=?", (tmp_c,))

    db.log_comment_action("Alice", "comment_add", pid, "Table 1", "please check")

    return {"pid": pid, "d1": d1, "d2": d2, "o1": o1, "o2": o2, "run": run,
            "f1": f1, "f2": f2, "c1": c1, "c2": c2, "c3": c3, "c4": c4, "a1": a1,
            "p1": p1, "p2": p2}


def _make_bundle(tables: dict, files: dict) -> bytes:
    """Hand-craft a bundle (for hostile/edge inputs)."""
    manifest = {
        "format": project_io.FORMAT, "version": 1, "exported_at": db.now_iso(),
        "app": "tlf_platform", "project_label": "x", "tables": tables,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("project.json", json.dumps(manifest))
        for arc, content in files.items():
            z.writestr(arc, content)
    return buf.getvalue()


def _minimal_project_row(project_id=1, name="safe"):
    return {
        "id": project_id, "compound": "C", "study": "S", "name": name,
        "edition_label": "", "created_at": db.now_iso(),
    }


def _minimal_document_row(document_id=9, project_id=1, filename="delivery.pdf"):
    return {
        "id": document_id, "project_id": project_id, "role": "delivery",
        "filename": filename, "n_pages": 1, "edition": "",
    }


def _minimal_output_row(output_id=20, project_id=1, document_id=9):
    return {
        "id": output_id, "project_id": project_id, "document_id": document_id,
        "seq": 0, "output_type": "Table", "number": "1", "label": "Table 1",
        "title": "Summary", "page_start": 1, "page_end": 1,
        "status": "Not Reviewed", "extraction_json": None, "content_hash": None,
        "src_hash": None, "judge_key": None,
    }


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #

def test_export_import_roundtrip_remaps_all_references(iso_db, safe_name):
    up = os.path.join(str(iso_db), "uploads")
    ids = _seed_project(up)

    data, warnings = project_io.export_bundle(ids["pid"])
    assert warnings == []                       # both PDFs present on disk

    res = project_io.import_bundle(data, up, safe_name, mode="new")
    npid = res["id"]
    assert npid != ids["pid"]
    assert res["counts"] == {"project": 1, "ai_run": 1, "document": 2, "output": 2,
                             "finding": 2, "comment": 4, "annotation": 1,
                             "audit_log": 5, "review_log": 1}

    ndocs = db.query("SELECT * FROM document WHERE project_id=? ORDER BY id", (npid,))
    nouts = db.query("SELECT * FROM output WHERE project_id=? ORDER BY id", (npid,))
    nfinds = db.query("SELECT * FROM finding WHERE project_id=? ORDER BY id", (npid,))
    ncoms = db.query("SELECT * FROM comment WHERE project_id=? ORDER BY id", (npid,))
    nannos = db.query("SELECT * FROM annotation WHERE output_id IN "
                      "(SELECT id FROM output WHERE project_id=?)", (npid,))
    nruns = db.query("SELECT * FROM ai_run WHERE project_id=? ORDER BY id", (npid,))
    naudit = db.query("SELECT * FROM audit_log WHERE project_id=? ORDER BY id", (npid,))
    nrev = db.query("SELECT * FROM review_log WHERE project_id=? ORDER BY id", (npid,))

    new_doc_ids = {d["id"] for d in ndocs}
    new_out_ids = {o["id"] for o in nouts}
    new_find_ids = {f["id"] for f in nfinds}
    new_com_ids = {c["id"] for c in ncoms}
    new_run_ids = {r["id"] for r in nruns}

    # FK integrity — every reference lands within the NEW project (or is None).
    assert all(o["document_id"] in new_doc_ids for o in nouts)
    for f in nfinds:
        assert f["output_id"] in new_out_ids or f["output_id"] is None
        assert f["run_id"] in new_run_ids
    for c in ncoms:
        assert c["output_id"] in new_out_ids or c["output_id"] is None
        assert c["finding_id"] in new_find_ids or c["finding_id"] is None
        assert c["parent_id"] in new_com_ids or c["parent_id"] is None
    assert nannos[0]["output_id"] in new_out_ids

    # The reply's parent_id points at the NEW parent comment, not the old id.
    reply = next(c for c in ncoms if c["body"] == "agreed")
    parent = next(c for c in ncoms if c["body"] == "please check")
    assert reply["parent_id"] == parent["id"] and reply["parent_id"] != ids["c1"]

    # AI comment -> its (remapped) finding.
    ai_com = next(c for c in ncoms if c["title"] == "AIW-2.1")
    normal_find = next(f for f in nfinds if f["check_id"] == "AIW-2.1")
    assert ai_com["finding_id"] == normal_find["id"] and ai_com["finding_id"] != ids["f1"]

    # Cross-output finding survives with output_id=None and a remapped run_id.
    xout = next(f for f in nfinds if f["check_id"] == "XOUT-001")
    assert xout["output_id"] is None and xout["run_id"] in new_run_ids

    # JSON columns + content_hash are byte-identical (opaque round-trip).
    assert normal_find["numbers"] == json.dumps([1240, 6, 1250])
    assert normal_find["subjects"] == json.dumps(["SYN-A102"])
    assert xout["affected"] == json.dumps(["Table 1", "Table 2"])
    t1 = next(o for o in nouts if o["label"] == "Table 1")
    assert t1["content_hash"] == "hash-abc"
    assert t1["extraction_json"] == '{"rows":[{"n":1240}]}'
    assert t1["status"] == "Approved"
    assert json.loads(nannos[0]["geom_json"]) == {
        "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4, "color": "#ffd54a",
    }

    # audit_log entity_id remapped by kind; the dangling target -> None.
    by_action = {a["action"]: a for a in naudit}
    assert by_action["project.create"]["entity_id"] == npid
    assert by_action["status.set"]["entity_id"] in new_out_ids
    assert by_action["finding.post"]["entity_id"] in new_find_ids
    com_audits = [a for a in naudit if a["action"] == "comment.add"]
    assert any(a["entity_id"] in new_com_ids for a in com_audits)
    assert any(a["entity_id"] is None for a in com_audits)      # deleted-target row

    # review_log carried across.
    assert nrev[0]["project_id"] == npid and nrev[0]["output_label"] == "Table 1"

    # PDF bytes identical and stored under the NEW project's upload folder.
    dmap = {d["role"]: d for d in ndocs}
    assert dmap["delivery"]["path"].startswith(os.path.join(up, str(npid)))
    assert open(dmap["delivery"]["path"], "rb").read() == open(ids["p1"], "rb").read()
    assert open(dmap["sap"]["path"], "rb").read() == open(ids["p2"], "rb").read()

    # Original project is untouched (no cross-talk).
    assert db.one("SELECT * FROM project WHERE id=?", (ids["pid"],)) is not None
    assert db.one("SELECT COUNT(*) c FROM output WHERE project_id=?", (ids["pid"],))["c"] == 2


def test_export_manifest_does_not_disclose_local_document_paths(iso_db):
    uploads = os.path.join(str(iso_db), "uploads")
    ids = _seed_project(uploads)

    data, warnings = project_io.export_bundle(ids["pid"])
    assert warnings == []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        manifest = json.loads(archive.read("project.json"))

    documents = manifest["tables"]["document"]
    assert documents
    assert all("path" not in row for row in documents)
    serialized = json.dumps(manifest)
    assert str(iso_db) not in serialized
    assert ids["p1"] not in serialized
    assert ids["p2"] not in serialized


def test_import_new_twice_makes_two_projects(iso_db, safe_name):
    up = os.path.join(str(iso_db), "uploads")
    ids = _seed_project(up)
    data, _ = project_io.export_bundle(ids["pid"])
    r1 = project_io.import_bundle(data, up, safe_name, mode="new")
    r2 = project_io.import_bundle(data, up, safe_name, mode="new")
    assert len({ids["pid"], r1["id"], r2["id"]}) == 3
    same = db.query("SELECT id FROM project WHERE compound='Compound Z' AND study='SYN-1' "
                    "AND name='Annual 2025'")
    assert len(same) == 3


# --------------------------------------------------------------------------- #
# Collision handling (ask / replace)
# --------------------------------------------------------------------------- #

def test_ask_mode_returns_conflict_without_writing(iso_db, safe_name):
    up = os.path.join(str(iso_db), "uploads")
    ids = _seed_project(up)
    data, _ = project_io.export_bundle(ids["pid"])
    before = set(os.listdir(up))
    n_before = len(db.query("SELECT id FROM project"))

    res = project_io.import_bundle(data, up, safe_name, mode="ask")
    assert res.get("conflict") is True
    assert res["existing"][0]["id"] == ids["pid"]
    assert len(db.query("SELECT id FROM project")) == n_before   # nothing written
    assert set(os.listdir(up)) == before                          # no new files


def test_replace_mode_swaps_project_and_removes_old_files(iso_db, safe_name):
    up = os.path.join(str(iso_db), "uploads")
    ids = _seed_project(up)
    data, _ = project_io.export_bundle(ids["pid"])

    res = project_io.import_bundle(data, up, safe_name, mode="replace")
    assert res["replaced"] == [ids["pid"]]
    # Old project gone (incl. its audit/review rows); exactly one remains.
    assert db.one("SELECT * FROM project WHERE id=?", (ids["pid"],)) is None
    assert db.query("SELECT id FROM audit_log WHERE project_id=?", (ids["pid"],)) == []
    assert db.query("SELECT id FROM review_log WHERE project_id=?", (ids["pid"],)) == []
    rows = db.query("SELECT id FROM project WHERE compound='Compound Z' AND study='SYN-1' "
                    "AND name='Annual 2025'")
    assert [r["id"] for r in rows] == [res["id"]]
    # Old upload folder removed; new one present.
    assert not os.path.isdir(os.path.join(up, str(ids["pid"])))
    assert os.path.isdir(os.path.join(up, str(res["id"])))


# --------------------------------------------------------------------------- #
# Robustness / edge cases
# --------------------------------------------------------------------------- #

def test_missing_pdf_is_warned_not_fatal(iso_db, safe_name):
    up = os.path.join(str(iso_db), "uploads")
    ids = _seed_project(up)
    os.remove(ids["p2"])                                    # one PDF vanishes on disk

    data, wexp = project_io.export_bundle(ids["pid"])
    assert any("document" in w.lower() for w in wexp)

    res = project_io.import_bundle(data, up, safe_name, mode="new")
    assert any("not in bundle" in w for w in res["warnings"])
    # Both document rows still imported; the surviving PDF is written.
    ndocs = db.query("SELECT * FROM document WHERE project_id=? ORDER BY id", (res["id"],))
    assert len(ndocs) == 2
    delivery = next(d for d in ndocs if d["role"] == "delivery")
    assert os.path.isfile(delivery["path"])


def test_hostile_filename_cannot_escape_uploads(iso_db, safe_name):
    up = os.path.join(str(iso_db), "uploads")
    tables = {
        "project": [{"id": 1, "compound": "C", "study": "S", "name": "trav",
                     "edition_label": "", "created_at": db.now_iso()}],
        "document": [{"id": 9, "project_id": 1, "role": "delivery",
                      "filename": "../../pwned.pdf", "n_pages": 1, "edition": ""}],
    }
    data = _make_bundle(tables, {"files/9/pwned.pdf": _pdf_bytes(1)})

    res = project_io.import_bundle(data, up, safe_name, mode="ask")   # no match -> imports
    doc = db.one("SELECT * FROM document WHERE project_id=?", (res["id"],))
    assert doc["path"].startswith(os.path.join(up, str(res["id"])))
    assert ".." not in os.path.relpath(doc["path"], up)
    assert os.path.isfile(doc["path"])
    assert not os.path.exists(os.path.join(str(iso_db), "pwned.pdf"))   # nothing escaped


def test_not_a_zip_is_rejected(iso_db, safe_name):
    with pytest.raises(ValueError):
        project_io.import_bundle(b"this is not a zip", os.path.join(str(iso_db), "u"), safe_name)


def test_missing_manifest_is_rejected(iso_db, safe_name):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("random.txt", "hi")
    with pytest.raises(ValueError):
        project_io.import_bundle(buf.getvalue(), os.path.join(str(iso_db), "u"), safe_name)


def test_bundle_must_have_exactly_one_project(iso_db, safe_name):
    data = _make_bundle({"project": []}, {})
    with pytest.raises(ValueError):
        project_io.import_bundle(data, os.path.join(str(iso_db), "u"), safe_name)


def test_bundle_rejects_oversized_single_entry_before_import(iso_db, safe_name, monkeypatch):
    data = _make_bundle({"project": []}, {"files/1/large.pdf": b"x" * 32})
    monkeypatch.setattr(project_io, "_MAX_ENTRY_UNCOMPRESSED", 31)

    with pytest.raises(ValueError, match="entry is too large"):
        project_io.import_bundle(data, os.path.join(str(iso_db), "u"), safe_name)


def test_bundle_rejects_oversized_total_before_import(iso_db, safe_name, monkeypatch):
    data = _make_bundle(
        {"project": []},
        {"files/1/a.pdf": b"a" * 24, "files/2/b.pdf": b"b" * 24},
    )
    monkeypatch.setattr(project_io, "_MAX_ENTRY_UNCOMPRESSED", 1024)
    monkeypatch.setattr(project_io, "_MAX_TOTAL_UNCOMPRESSED", 47)

    with pytest.raises(ValueError, match="bundle is too large"):
        project_io.import_bundle(data, os.path.join(str(iso_db), "u"), safe_name)


def test_bundle_rejects_extreme_compression_ratio(iso_db, safe_name, monkeypatch):
    manifest = {"format": project_io.FORMAT, "version": 1, "tables": {"project": []}}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("project.json", json.dumps(manifest))
        z.writestr("files/1/compressible.pdf", b"a" * 4096)
    monkeypatch.setattr(project_io, "_RATIO_CHECK_MIN_BYTES", 1)
    monkeypatch.setattr(project_io, "_MAX_COMPRESSION_RATIO", 2)

    with pytest.raises(ValueError, match="unsafe compression ratio"):
        project_io.import_bundle(buf.getvalue(), os.path.join(str(iso_db), "u"), safe_name)


def test_bundle_rejects_duplicate_member_names(iso_db, safe_name):
    manifest = {"format": project_io.FORMAT, "version": 1, "tables": {"project": []}}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("project.json", json.dumps(manifest))
        with pytest.warns(UserWarning):
            z.writestr("project.json", json.dumps(manifest))

    with pytest.raises(ValueError, match="duplicate member names"):
        project_io.import_bundle(buf.getvalue(), os.path.join(str(iso_db), "u"), safe_name)


def test_bundle_rejects_duplicate_manifest_keys(iso_db, safe_name):
    # Python's default json loader silently keeps the last duplicate key. A bundle manifest is a security boundary, so ambiguous JSON is rejected instead.
    raw = (
        '{"format":"tlf_project_bundle","format":"tlf_project_bundle",'
        '"version":1,"tables":{"project":[]}}'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("project.json", raw)
    with pytest.raises(ValueError, match="not valid JSON"):
        project_io.import_bundle(buf.getvalue(), os.path.join(str(iso_db), "u"), safe_name)


@pytest.mark.parametrize("bad_json", ['{"coverage":NaN}', '{"coverage":1,"coverage":2}'])
def test_bundle_rejects_ambiguous_or_nonfinite_cached_extraction(
    iso_db, safe_name, bad_json,
):
    output = _minimal_output_row()
    output["extraction_json"] = bad_json
    tables = {
        "project": [_minimal_project_row()],
        "document": [_minimal_document_row()],
        "output": [output],
    }
    data = _make_bundle(tables, {"files/9/delivery.pdf": _pdf_bytes(1)})
    with pytest.raises(ValueError, match="extraction_json"):
        project_io.import_bundle(data, os.path.join(str(iso_db), "u"), safe_name)


def test_bundle_rejects_non_list_table_and_row_schema_drift(iso_db, safe_name):
    bad_table = _make_bundle({"project": {"id": 1}}, {})
    with pytest.raises(ValueError, match="must be a list"):
        project_io.import_bundle(bad_table, os.path.join(str(iso_db), "u"), safe_name)

    project = _minimal_project_row()
    del project["created_at"]
    bad_row = _make_bundle({"project": [project]}, {})
    with pytest.raises(ValueError, match="versioned schema"):
        project_io.import_bundle(bad_row, os.path.join(str(iso_db), "u"), safe_name)


def test_bundle_rejects_unknown_foreign_key_and_markup_status(iso_db, safe_name):
    output = _minimal_output_row(document_id=999)
    tables = {
        "project": [_minimal_project_row()],
        "document": [_minimal_document_row()],
        "output": [output],
    }
    with pytest.raises(ValueError, match="unknown document"):
        project_io.import_bundle(
            _make_bundle(tables, {"files/9/delivery.pdf": _pdf_bytes(1)}),
            os.path.join(str(iso_db), "u"), safe_name,
        )

    output["document_id"] = 9
    output["status"] = '\"><img src=x onerror=alert(1)>'
    with pytest.raises(ValueError, match="invalid output status"):
        project_io.import_bundle(
            _make_bundle(tables, {"files/9/delivery.pdf": _pdf_bytes(1)}),
            os.path.join(str(iso_db), "u"), safe_name,
        )


def test_bundle_rejects_invalid_import_mode_before_writing(iso_db, safe_name):
    data = _make_bundle({"project": [_minimal_project_row()]}, {})
    with pytest.raises(ValueError, match="import mode"):
        project_io.import_bundle(data, os.path.join(str(iso_db), "u"), safe_name, mode="overwrite")
    assert db.query("SELECT id FROM project") == []


def test_import_gives_sanitized_filename_collisions_distinct_paths(iso_db, safe_name):
    tables = {
        "project": [_minimal_project_row()],
        "document": [
            _minimal_document_row(9, filename="../same.pdf"),
            _minimal_document_row(10, filename="same.pdf"),
        ],
    }
    data = _make_bundle(
        tables,
        {"files/9/a.pdf": _pdf_bytes(1), "files/10/b.pdf": _pdf_bytes(1)},
    )
    result = project_io.import_bundle(data, os.path.join(str(iso_db), "u"), safe_name, mode="new")
    documents = db.query("SELECT filename, path FROM document WHERE project_id=? ORDER BY id", (result["id"],))
    assert len({row["filename"].casefold() for row in documents}) == 2
    assert len({row["path"].casefold() for row in documents}) == 2
    assert all(os.path.isfile(row["path"]) for row in documents)


def test_import_collision_suffix_cannot_overwrite_an_existing_name(iso_db, safe_name):
    first = _minimal_document_row(9, filename="same.pdf")
    second = _minimal_document_row(10, filename="same_11.pdf")
    third = _minimal_document_row(11, filename="same.pdf")
    first["n_pages"], second["n_pages"], third["n_pages"] = 1, 2, 3
    tables = {
        "project": [_minimal_project_row()],
        "document": [first, second, third],
    }
    data = _make_bundle(
        tables,
        {
            "files/9/a.pdf": _pdf_bytes(1),
            "files/10/b.pdf": _pdf_bytes(2),
            "files/11/c.pdf": _pdf_bytes(3),
        },
    )
    result = project_io.import_bundle(data, os.path.join(str(iso_db), "u"), safe_name, mode="new")
    documents = db.query(
        "SELECT filename, path, n_pages FROM document WHERE project_id=? ORDER BY id",
        (result["id"],),
    )
    assert [row["n_pages"] for row in documents] == [1, 2, 3]
    assert len({row["filename"].casefold() for row in documents}) == 3
    assert len({row["path"].casefold() for row in documents}) == 3
    assert all(os.path.isfile(row["path"]) for row in documents)


def test_bundle_rejects_multiple_members_for_one_document(iso_db, safe_name):
    manifest = {
        "format": project_io.FORMAT, "version": 1, "exported_at": db.now_iso(),
        "app": "tlf_platform", "project_label": "x",
        "tables": {
            "project": [_minimal_project_row()],
            "document": [_minimal_document_row()],
        },
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("project.json", json.dumps(manifest))
        archive.writestr("files/9/a.pdf", _pdf_bytes(1))
        archive.writestr("files/9/b.pdf", _pdf_bytes(1))
    with pytest.raises(ValueError, match="multiple files"):
        project_io.import_bundle(buf.getvalue(), os.path.join(str(iso_db), "u"), safe_name)


def test_import_is_atomic_on_midway_failure(iso_db, safe_name, monkeypatch):
    up = os.path.join(str(iso_db), "uploads")
    ids = _seed_project(up)
    data, _ = project_io.export_bundle(ids["pid"])

    before_dirs = set(os.listdir(up))
    n_audit = len(db.query("SELECT id FROM audit_log"))
    orig = project_io._insert

    def boom(conn, table, cols):
        if table == "finding":
            raise RuntimeError("injected mid-import failure")
        return orig(conn, table, cols)

    monkeypatch.setattr(project_io, "_insert", boom)
    with pytest.raises(RuntimeError):
        project_io.import_bundle(data, up, safe_name, mode="new")

    # No half-project: rows rolled back, would-be upload dir cleaned up.
    assert [r["id"] for r in db.query("SELECT id FROM project ORDER BY id")] == [ids["pid"]]
    assert len(db.query("SELECT id FROM audit_log")) == n_audit
    assert set(os.listdir(up)) == before_dirs
