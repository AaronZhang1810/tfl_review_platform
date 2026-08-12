"""Tests for the PARALLEL per-table review loop in runner._do_run.

The run analyses tables concurrently, which buys most of the speed-up but also
introduces the three things that could silently regress:

  * ORDER — findings must be inserted in target order, not completion order, so
    finding ids (and the UI's grouping) stay deterministic run to run.
  * ERROR CONTAINMENT — one table raising a normal error must not collapse the run,
    while a connection blackout still aborts it loudly.
  * COVERAGE — a table truncated by the slice cap must be reported in the summary,
    because a partial extraction otherwise looks like a clean table.

The AI itself is stubbed; what is under test is the orchestration.
"""

import concurrent.futures as cf
import threading
import time

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Temp DB + one project with 6 target tables, and every AI call stubbed."""
    import db
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    if hasattr(db._local, "conn"):
        del db._local.conn
    db.init()

    import ai_review, pdftools, runner

    pid = db.insert("project", compound="C", study="S", name="P",
                    edition_label="Annual 2025", created_at=db.now_iso())
    doc = db.insert("document", project_id=pid, role="delivery",
                    filename="cur.pdf", path=str(tmp_path / "cur.pdf"),
                    n_pages=100, edition="2025")
    labels = [f"Table {i}" for i in range(1, 7)]
    for seq, lbl in enumerate(labels, start=1):
        db.insert("output", project_id=pid, document_id=doc, seq=seq,
                  output_type="Table", number=str(seq), label=lbl, title=f"t{seq}",
                  page_start=seq, page_end=seq + 9, status="Not Reviewed")
    targets = db.query("SELECT * FROM output WHERE project_id=? ORDER BY seq", (pid,))
    current = db.one("SELECT * FROM document WHERE id=?", (doc,))

    # --- stub every AI touchpoint -------------------------------------------- #
    monkeypatch.setattr(ai_review, "load_config",
                        lambda: {"dedupe_findings": False, "structural_checks": {}})
    monkeypatch.setattr(pdftools, "range_text", lambda *a, **k: "")
    # _analyze_table now reads page text once and reuses it, so page_texts (not range_text) is the call that must be stubbed away from disk.
    monkeypatch.setattr(pdftools, "page_texts",
                        lambda path, a, b: ["row 1 (50.0)" for _ in range(a, b + 1)])
    monkeypatch.setattr(ai_review, "self_check", lambda text, ex, cfg: (ex, 0, 0))
    monkeypatch.setattr(ai_review, "within_table_findings", lambda ex, lbl, cfg: [])
    monkeypatch.setattr(ai_review, "verify_findings", lambda fs, cfg: (fs, 0))
    monkeypatch.setattr(ai_review, "cross_output_judge", lambda bundle, cfg: [])
    return {"db": db, "runner": runner, "ai_review": ai_review,
            "pid": pid, "doc": doc, "labels": labels,
            "targets": targets, "current": current, "tmp": tmp_path}


def _mk_run(env):
    return env["db"].insert("ai_run", project_id=env["pid"], kind="incremental",
                            started_at=env["db"].now_iso(), summary_json="{}")


def _extraction(pages_total=10, pages_read=10):
    return {"summary_rows": [{"label": "Participants", "values": {"Total": 10}}],
            "groups": [{"label": "Total", "n": 10}],
            "coverage": {"pages_total": pages_total, "pages_read": pages_read,
                         "slices_total": pages_total, "slices_used": pages_read,
                         "truncated": pages_read < pages_total}}


def test_findings_persist_and_read_back_in_output_order(env, monkeypatch):
    """Workers now persist findings in completion order (later tables can finish first), so the DISPLAY order must come from the query (by output seq), not insert order — and every table's finding must be durably written."""
    runner, db = env["runner"], env["db"]
    n = len(env["targets"])
    monkeypatch.setattr(runner, "_extraction_for", lambda o, pages=None: _extraction())

    def judge(ex, pri_ex, label, title, cfg):
        idx = env["labels"].index(label)
        time.sleep(0.02 * (n - idx))     # reverse completion order
        return [{"check_id": "AIW-2.1", "message": f"m {label}", "severity": "high",
                 "risk": "High", "numbers": [], "subjects": []}]
    monkeypatch.setattr(env["ai_review"], "within_table_judge", judge)

    rid = _mk_run(env)
    runner.RUN_PROGRESS[env["pid"]] = {"running": True, "done": 0, "total": n,
                                       "message": "", "run_id": rid, "skipped": 0,
                                       "errors": []}
    runner._do_run(env["pid"], rid, "incremental", env["current"], None, env["targets"])

    # Every table judged, one finding each, all phase 'within'.
    assert runner.RUN_PROGRESS[env["pid"]]["summary"]["findings"] == n
    assert db.one("SELECT COUNT(*) c FROM finding WHERE project_id=? AND phase='within'",
                  (env["pid"],))["c"] == n
    # The API's ORDER BY (output seq) yields target order regardless of who finished first.
    rows = db.query("SELECT o.label FROM finding f JOIN output o ON o.id=f.output_id "
                    "WHERE f.project_id=? ORDER BY COALESCE(o.seq, 1000000), f.check_id, f.id",
                    (env["pid"],))
    assert [r["label"] for r in rows] == env["labels"]


def test_tables_really_run_concurrently(env, monkeypatch):
    """More than one table must be in flight at once (the whole point of the change)."""
    runner, ai_review = env["runner"], env["ai_review"]
    monkeypatch.setattr(runner, "_extraction_for", lambda o, pages=None: _extraction())
    live, peak, lock = 0, 0, threading.Lock()

    def judge(ex, pri_ex, label, title, cfg):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.25)      # wide enough that overlap is reliable even on a loaded box
        with lock:
            live -= 1
        return []
    monkeypatch.setattr(ai_review, "within_table_judge", judge)

    rid = _mk_run(env)
    runner.RUN_PROGRESS[env["pid"]] = {"running": True, "done": 0,
                                       "total": len(env["targets"]), "message": "",
                                       "run_id": rid, "skipped": 0, "errors": []}
    runner._do_run(env["pid"], rid, "incremental", env["current"], None, env["targets"])
    assert peak > 1, f"tables ran serially (peak concurrency {peak})"


def test_one_table_error_is_contained(env, monkeypatch):
    """A normal error on one table must not lose the other five."""
    runner, db = env["runner"], env["db"]
    monkeypatch.setattr(runner, "_extraction_for", lambda o, pages=None: _extraction())

    def judge(ex, pri_ex, label, title, cfg):
        if label == "Table 3":
            raise ValueError("boom")
        return [{"check_id": "AIW-2.1", "message": "m", "severity": "minor",
                 "numbers": [], "subjects": []}]
    monkeypatch.setattr(env["ai_review"], "within_table_judge", judge)

    rid = _mk_run(env)
    runner.RUN_PROGRESS[env["pid"]] = {"running": True, "done": 0,
                                       "total": len(env["targets"]), "message": "",
                                       "run_id": rid, "skipped": 0, "errors": []}
    runner._do_run(env["pid"], rid, "incremental", env["current"], None, env["targets"])

    s = runner.RUN_PROGRESS[env["pid"]]["summary"]
    assert s["findings"] == len(env["targets"]) - 1
    assert s["n_judge_failed"] == 1
    assert any("Table 3" in e and "boom" in e for e in s["errors"])
    assert not s["ai_unreachable"]


def test_connection_blackout_aborts_the_run(env, monkeypatch):
    """A connection error must still fail the whole run loudly, not half-succeed."""
    runner = env["runner"]
    import ai_client
    monkeypatch.setattr(runner, "_extraction_for", lambda o, pages=None: _extraction())
    monkeypatch.setattr(ai_client, "is_connection_error", lambda e: "APIConnection" in str(e))

    def judge(ex, pri_ex, label, title, cfg):
        raise RuntimeError("APIConnection: gateway down")
    monkeypatch.setattr(env["ai_review"], "within_table_judge", judge)

    rid = _mk_run(env)
    runner.RUN_PROGRESS[env["pid"]] = {"running": True, "done": 0,
                                       "total": len(env["targets"]), "message": "",
                                       "run_id": rid, "skipped": 0, "errors": []}
    runner._do_run(env["pid"], rid, "incremental", env["current"], None, env["targets"])

    prog = runner.RUN_PROGRESS[env["pid"]]
    assert prog["running"] is False
    assert prog["message"].startswith("error:"), prog["message"]
    # No findings may be committed from a blacked-out run.
    assert env["db"].query("SELECT id FROM finding WHERE project_id=?", (env["pid"],)) == []


def test_truncated_table_is_reported_in_the_summary(env, monkeypatch):
    """The silent-partial-read failure mode: coverage must reach the summary."""
    runner = env["runner"]

    def extraction_for(o, pages=None):
        if o["label"] == "Table 2":
            return _extraction(pages_total=92, pages_read=14)   # truncated
        return _extraction()
    monkeypatch.setattr(runner, "_extraction_for", extraction_for)
    monkeypatch.setattr(env["ai_review"], "within_table_judge",
                        lambda ex, pri, lbl, t, cfg: [])

    rid = _mk_run(env)
    runner.RUN_PROGRESS[env["pid"]] = {"running": True, "done": 0,
                                       "total": len(env["targets"]), "message": "",
                                       "run_id": rid, "skipped": 0, "errors": []}
    runner._do_run(env["pid"], rid, "incremental", env["current"], None, env["targets"])

    cov = runner.RUN_PROGRESS[env["pid"]]["summary"]["coverage"]
    assert cov["n_truncated"] == 1
    assert cov["truncated"][0]["label"] == "Table 2"
    assert cov["truncated"][0]["pages_read"] == 14
    assert cov["truncated"][0]["pages_total"] == 92
    # 5 full tables (10pp each) + the truncated one (14 of 92)
    assert cov["pages_read"] == 5 * 10 + 14
    assert cov["pages_total"] == 5 * 10 + 92


# --------------------------------------------------------------------------- #
# Auto-approve clean tables: after an AI run, a fully-read table with no findings flips Not Reviewed -> Auto-approved. A table with a finding, a partially-read table, and a status a human already set are all left alone.
# --------------------------------------------------------------------------- #

def test_clean_fully_read_tables_are_auto_approved(env, monkeypatch):
    runner, db = env["runner"], env["db"]

    def extraction_for(o, pages=None):
        if o["label"] == "Table 5":
            return _extraction(pages_total=92, pages_read=14)   # truncated -> ineligible
        return _extraction()
    monkeypatch.setattr(runner, "_extraction_for", extraction_for)

    def judge(ex, pri, label, title, cfg):
        if label == "Table 1":
            return [{"check_id": "AIW-2.1", "message": "m", "severity": "high",
                     "risk": "High", "numbers": [], "subjects": []}]
        return []                                               # every other table is clean
    monkeypatch.setattr(env["ai_review"], "within_table_judge", judge)

    rid = _mk_run(env)
    runner.RUN_PROGRESS[env["pid"]] = {"running": True, "done": 0,
                                       "total": len(env["targets"]), "message": "",
                                       "run_id": rid, "skipped": 0, "errors": []}
    runner._do_run(env["pid"], rid, "incremental", env["current"], None, env["targets"])

    st = {o["label"]: o["status"] for o in
          db.query("SELECT label, status FROM output WHERE project_id=?", (env["pid"],))}
    # Fail closed at RUN scope: because one required table was only partially read, no zero-finding table is represented as a completed comprehensive review.
    assert set(st.values()) == {"Not Reviewed"}
    summary = runner.RUN_PROGRESS[env["pid"]]["summary"]
    assert summary["auto_approved"] == 0
    assert summary["status"] == "partial"
    assert summary["review_complete"] is False


def test_auto_approve_never_overrides_a_human_status(env, monkeypatch):
    """Only 'Not Reviewed' is touched — a status a reviewer set is preserved."""
    runner, db = env["runner"], env["db"]
    monkeypatch.setattr(runner, "_extraction_for", lambda o, pages=None: _extraction())
    monkeypatch.setattr(env["ai_review"], "within_table_judge",
                        lambda ex, pri, lbl, t, cfg: [])       # all tables clean
    # A reviewer already moved Table 2 to In Progress.
    db.execute("UPDATE output SET status='In Progress' WHERE id=?", (env["targets"][1]["id"],))

    rid = _mk_run(env)
    runner.RUN_PROGRESS[env["pid"]] = {"running": True, "done": 0,
                                       "total": len(env["targets"]), "message": "",
                                       "run_id": rid, "skipped": 0, "errors": []}
    runner._do_run(env["pid"], rid, "incremental", env["current"], None, env["targets"])

    st = {o["label"]: o["status"] for o in
          db.query("SELECT label, status FROM output WHERE project_id=?", (env["pid"],))}
    assert st["Table 2"] == "In Progress"       # human status untouched
    assert st["Table 1"] == "Auto-approved"     # clean & Not Reviewed -> auto-approved
    assert runner.RUN_PROGRESS[env["pid"]]["summary"]["auto_approved"] == 5


# --------------------------------------------------------------------------- #
# Coverage honesty: a slice that ERRORS must not be counted as a page read. Without this, a run where half the page-reads failed still reported 100% coverage — presenting a partial extraction as a complete review.
# --------------------------------------------------------------------------- #

def test_coverage_counts_only_pages_that_actually_merged(monkeypatch):
    import ai_review
    fail = {5, 6, 7, 8, 9, 10}

    def flaky(text, label, groups, page):
        if page in fail:
            raise ValueError("transient slice failure")
        return {"groups": [{"label": "G1", "n": 9}] if page == 1 else [],
                "summary_rows": [{"label": f"r{page}", "values": {"G1": 1}, "page": page}],
                "footnote_markers": [], "pt_terms": [], "missing_n_rows": [], "notes": "",
                "analysis_set": "", "run_datetime": "", "header_label": label,
                "is_ae_pt_table": False}
    monkeypatch.setattr(ai_review, "_extract_slice", flaky)
    monkeypatch.setattr(ai_review, "_MAX_SLICES", 100)

    ex = ai_review._extract_paged(["x" * 900 for _ in range(20)], "Table Z")
    cov = ex["coverage"]
    assert cov["pages_read"] == 20 - len(fail)      # NOT 20
    assert cov["incomplete"] is True                # something is missing...
    assert cov["truncated"] is False                # ...but the slice cap wasn't the cause
    # Merged rows stay in page order and exclude the failed pages.
    assert [r["label"] for r in ex["summary_rows"]] == \
           [f"r{p}" for p in range(1, 21) if p not in fail]


def test_slice_cap_sets_truncated_not_just_incomplete(monkeypatch):
    """Capping and failing are different causes; the UI advises differently."""
    import ai_review
    monkeypatch.setattr(ai_review, "_extract_slice",
                        lambda text, label, groups, page: {
                            "groups": [], "summary_rows": [], "footnote_markers": [],
                            "pt_terms": [], "missing_n_rows": [], "notes": "",
                            "analysis_set": "", "run_datetime": "", "header_label": label,
                            "is_ae_pt_table": False})
    monkeypatch.setattr(ai_review, "_MAX_SLICES", 5)
    cov = ai_review._extract_paged(["x" * 900 for _ in range(20)], "Table Z")["coverage"]
    assert cov["truncated"] is True and cov["incomplete"] is True
    assert cov["pages_read"] == 5


def test_cache_key_covers_the_whole_table_not_just_the_prompt_window(env, monkeypatch):
    """The content hash is the cache-invalidation key: hashing only the first 60k chars left most of a long table outside it, so an edit on a later page went unnoticed."""
    import pdftools, runner
    db = env["db"]
    o = env["targets"][0]
    pages = ["p%d " % i + "z" * 5000 for i in range(1, 41)]   # ~200k chars, >60k window
    monkeypatch.setattr(pdftools, "page_texts", lambda *a, **k: pages)
    monkeypatch.setattr(pdftools, "content_hash", lambda t: f"len={len(t)}")
    monkeypatch.setattr(env["ai_review"], "extract",
                        lambda text, label, page_texts=None: {"summary_rows": [], "groups": []})
    runner._extraction_for(o)
    stored = db.one("SELECT content_hash FROM output WHERE id=?", (o["id"],))["content_hash"]
    joined_pages = "\n".join(pages)
    assert stored == f"len={len(joined_pages)}"


def test_page_text_is_read_once_per_table(env, monkeypatch):
    """Extraction and the self-check used to pull the same pages independently, doubling the (GIL-bound) pdfplumber cost of every table."""
    import pdftools, runner
    calls = {"n": 0}

    def counting(path, a, b):
        calls["n"] += 1
        return ["row 1 (50.0)" for _ in range(a, b + 1)]
    monkeypatch.setattr(pdftools, "page_texts", counting)
    monkeypatch.setattr(pdftools, "content_hash", lambda t: "h")
    monkeypatch.setattr(env["ai_review"], "extract",
                        lambda text, label, page_texts=None: _extraction())
    monkeypatch.setattr(env["ai_review"], "within_table_judge",
                        lambda ex, pri, lbl, t, cfg: [])
    runner._analyze_table(env["targets"][0], env["ai_review"].load_config(), {},
                          {"model": None, "effort": None}, _mk_run(env), threading.Event())
    assert calls["n"] == 1, f"page text read {calls['n']}x per table"


# --------------------------------------------------------------------------- #
# The no-PDF-read fast path. Reading page text costs ~0.6 s/page (pdfplumber) and holds the GIL; on an unchanged two-edition delivery that was ~18 min per run spent only to re-derive a cache key. src_hash (a digest of the FILE BYTES) lets an unchanged file skip the read entirely, while any byte change still invalidates.
# --------------------------------------------------------------------------- #

@pytest.fixture()
def fastpath(tmp_path, monkeypatch):
    import db
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "fp.db"))
    if hasattr(db._local, "conn"):
        del db._local.conn
    db.init()
    import pdftools, ai_review, runner

    pdf = tmp_path / "cur.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake bytes " * 500)      # real file, so file_hash works
    reads = {"n": 0}

    def counting(path, a, b):
        reads["n"] += 1
        return ["Any adverse event 10 (50.0)" for _ in range(a, b + 1)]
    monkeypatch.setattr(pdftools, "page_texts", counting)

    cfg = {"dedupe_findings": False, "structural_checks": {},
           "selfcheck": {"enabled": True, "pct_tolerance": 0.6}}
    monkeypatch.setattr(ai_review, "load_config", lambda: cfg)
    monkeypatch.setattr(ai_review, "extract",
                        lambda text, label, page_texts=None: {"summary_rows": [], "groups": []})
    monkeypatch.setattr(ai_review, "within_table_findings", lambda ex, l, c: [])
    monkeypatch.setattr(ai_review, "within_table_judge", lambda ex, p, l, t, c: [])
    monkeypatch.setattr(ai_review, "verify_findings", lambda fs, c: (fs, 0))

    pid = db.insert("project", compound="C", study="S", name="P", created_at=db.now_iso())
    doc = db.insert("document", project_id=pid, role="delivery", filename="cur.pdf",
                    path=str(pdf), n_pages=10, edition="2025")
    db.insert("output", project_id=pid, document_id=doc, seq=1, output_type="Table",
              number="1", label="Table 1", title="t", page_start=1, page_end=10,
              status="Not Reviewed")
    o = db.query("SELECT * FROM output")[0]
    rid = db.insert("ai_run", project_id=pid, kind="incremental",
                    started_at=db.now_iso(), summary_json="{}")
    run = lambda: runner._analyze_table(o, cfg, {}, {"model": None, "effort": None},
                                        rid, threading.Event())
    return {"db": db, "runner": runner, "ai_review": ai_review, "pdftools": pdftools,
            "o": o, "pdf": pdf, "reads": reads, "cfg": cfg, "run": run}


def test_unchanged_file_skips_the_pdf_read_entirely(fastpath):
    f = fastpath
    f["run"]()                                   # cold: must read
    assert f["reads"]["n"] == 1
    assert f["db"].one("SELECT src_hash FROM output WHERE id=?", (f["o"]["id"],))["src_hash"]
    f["reads"]["n"] = 0
    f["run"]()                                   # warm: must NOT read
    assert f["reads"]["n"] == 0


def test_byte_change_invalidates_the_fast_path(fastpath):
    f = fastpath
    f["run"]()
    f["pdf"].write_bytes(f["pdf"].read_bytes() + b"EXTRA")   # one byte differs
    f["reads"]["n"] = 0
    f["run"]()
    assert f["reads"]["n"] >= 1, "a changed PDF must not serve a cached extraction"


def test_suspect_cells_still_force_a_text_read_on_a_cache_hit(fastpath, monkeypatch):
    """The precision guard is not skipped: if the extraction has inconsistent cells, the source text is read so self_check can re-read them, cache hit or not."""
    f = fastpath
    import json
    bad = {"groups": [{"label": "G1", "n": 10}],
           "summary_rows": [{"label": "r", "values": {"G1": 99},
                             "pcts": {"G1": 1.0}, "page": 1}]}
    assert f["ai_review"].needs_self_check(bad, f["cfg"]) is True
    f["db"].execute("UPDATE output SET extraction_json=?, src_hash=? WHERE id=?",
                    (json.dumps(bad), f["pdftools"].file_hash(str(f["pdf"])), f["o"]["id"]))
    calls = {"n": 0}

    def sc(text, ex, c):
        calls["n"] += 1
        assert text, "self_check must receive the source text"
        return ex, 1, 0
    monkeypatch.setattr(f["ai_review"], "self_check", sc)
    f["reads"]["n"] = 0
    f["run"]()
    assert f["reads"]["n"] == 1 and calls["n"] == 1


def test_file_hash_is_byte_exact_and_cached(tmp_path):
    import pdftools
    p = tmp_path / "a.bin"
    p.write_bytes(b"x" * 1000)
    h1 = pdftools.file_hash(str(p))
    assert pdftools.file_hash(str(p)) == h1          # cached, same answer
    p.write_bytes(b"x" * 999 + b"y")                 # same length, one byte differs
    assert pdftools.file_hash(str(p)) != h1, "size+mtime alone would have missed this"


# --------------------------------------------------------------------------- #
# Cross-output bundle: EVERY row must reach the judge.
#
# _compact used to keep only rows matching _KEYROW_RE, which on real deliveries selected 1-5 rows per table — a 295-row SOC/PT table contributed one row, so checklist item 7.1 (AE overview vs SOC/PT) had nothing to reconcile against. All rows now go, which exceeds one context window, so the bundle is chunked.
# --------------------------------------------------------------------------- #

def _ex(n_rows, label_prefix="PT"):
    return {"groups": [{"label": "G1", "n": 9}], "footnote_markers": ["fn"],
            "summary_rows": [{"label": f"{label_prefix} {i}", "values": {"G1": i},
                              "pcts": {"G1": 1.0}, "section": "Condition Alpha", "page": i,
                              "row_kind": "study"} for i in range(1, n_rows + 1)]}


def test_compact_sends_every_row_and_drops_only_unused_fields():
    import ai_review
    c = ai_review._compact(_ex(300))
    assert len(c["rows"]) == 300, "a 300-row table must contribute 300 rows"
    assert set(c["rows"][0]) == {"label", "values", "section", "page"}
    # cross-output items compare counts and group N's; these two were most of the bytes
    assert "pcts" not in c["rows"][0] and "row_kind" not in c["rows"][0]
    # the old keyrow-capped projection is still reachable for callers that want it
    assert len(ai_review._compact(_ex(300), max_rows=10)["rows"]) == 10


def test_chunk_bundle_keeps_the_hub_in_every_chunk_and_each_table_once(monkeypatch):
    import ai_review, runner
    monkeypatch.setattr(runner, "_XOUT_CHUNK_CHARS", 60_000)   # force several chunks
    bundle = []
    for lbl in ["Table 1"] + [f"Table {i}" for i in range(2, 12)]:
        e = ai_review._compact(_ex(120))
        e["label"], e["title"] = lbl, "t"
        bundle.append(e)
    chunks = runner._chunk_bundle(bundle)
    assert len(chunks) > 1, "should have split"
    # items 3/4/8 all reconcile against Table 1, so it must be in every chunk
    for ch in chunks:
        assert any(runner._norm_label(e["label"]) == "table 1" for e in ch)
    # every other table appears exactly once — no row is dropped, none duplicated
    others = [e["label"] for ch in chunks for e in ch
              if runner._norm_label(e["label"]) != "table 1"]
    assert sorted(others) == sorted(b["label"] for b in bundle
                                    if runner._norm_label(b["label"]) != "table 1")
    assert len(others) == len(set(others))


def test_chunk_bundle_edge_cases(monkeypatch):
    import runner
    assert runner._chunk_bundle([]) == []
    hub = {"label": "Table 1", "rows": []}
    assert runner._chunk_bundle([hub]) == [[hub]]              # hub only
    solo = {"label": "Table 9", "rows": []}
    assert runner._chunk_bundle([solo]) == [[solo]]            # no hub at all
    # a single table larger than the whole budget still gets its own chunk
    monkeypatch.setattr(runner, "_XOUT_CHUNK_CHARS", 10)
    big = {"label": "Table 9", "rows": [{"label": "x" * 500}]}
    assert runner._chunk_bundle([hub, big]) == [[hub, big]]


def test_every_chunk_is_judged_and_findings_are_merged(env, monkeypatch):
    """All chunks must be judged (a skipped chunk = silently unreviewed tables), and their findings merged into one deduped set."""
    runner, db = env["runner"], env["db"]
    monkeypatch.setattr(runner, "_extraction_for", lambda o, pages=None: _extraction())
    monkeypatch.setattr(env["ai_review"], "within_table_judge",
                        lambda ex, p, l, t, c: [])
    monkeypatch.setattr(runner, "_XOUT_CHUNK_CHARS", 1)   # one chunk per table
    seen = []

    def judge(chunk, cfg):
        seen.append([e["label"] for e in chunk])
        return [{"check_id": "AIX-3", "message": f"x{len(seen)}", "severity": "major",
                 "numbers": [], "subjects": [], "affected": [chunk[-1]["label"]]}]
    monkeypatch.setattr(env["ai_review"], "cross_output_judge", judge)

    rid = _mk_run(env)
    runner.RUN_PROGRESS[env["pid"]] = {"running": True, "done": 0,
                                       "total": len(env["targets"]), "message": "",
                                       "run_id": rid, "skipped": 0, "errors": []}
    runner._do_run(env["pid"], rid, "incremental", env["current"], None, env["targets"])
    # The hub rides along in every chunk rather than forming one of its own, so with N tables there are N-1 chunks. The invariant that matters: no table goes unjudged.
    labels = {o["label"] for o in env["targets"]}
    covered = {lbl for ch in seen for lbl in ch}
    assert covered == labels, f"tables never judged: {sorted(labels - covered)}"
    assert len(seen) == len(labels) - 1
    # every chunk's findings were merged and stored
    xf = db.query("SELECT check_id FROM finding WHERE project_id=? AND check_id='AIX-3'",
                  (env["pid"],))
    assert len(xf) == len(seen)


def test_chunk_boundaries_never_split_a_table_family(monkeypatch):
    """Item 7.1 reconciles an AE-overview table against its SOC/PT sibling. If a chunk boundary fell between them, no single call would see both and the comparison would silently not happen — so families must stay whole."""
    import ai_review, runner
    monkeypatch.setattr(runner, "_XOUT_CHUNK_CHARS", 40_000)
    bundle = []
    for lbl in (["Table 1"]
                + [f"Table 2.2.{i}" for i in range(1, 5)]
                + [f"Table 3.2.{i}" for i in range(1, 5)]
                + [f"Table 4.5.{i}" for i in range(1, 5)]):
        e = ai_review._compact(_ex(90))
        e["label"], e["title"] = lbl, "t"
        bundle.append(e)
    chunks = runner._chunk_bundle(bundle)
    assert len(chunks) > 1, "should have split"
    where = {}
    for i, ch in enumerate(chunks):
        for e in ch:
            if runner._norm_label(e["label"]) == "table 1":
                continue
            where.setdefault(runner._family(e["label"]), set()).add(i)
    split = {f: ix for f, ix in where.items() if len(ix) > 1}
    assert not split, f"families split across chunks: {sorted(split)}"
    assert set(where) == {"2.2", "3.2", "4.5"}


def test_family_extraction():
    import runner
    assert runner._family("Table 2.2.1") == "2.2"
    assert runner._family("Table 8.1.1") == "8.1"
    assert runner._family("Table 1") == "1"
    assert runner._family("Listing A") == "listing a"      # no number → own family


# --------------------------------------------------------------------------- #
# Continuability: a completed table is durable (judge_key stamped), so a re-run or a crash-resume skips it and only does the outstanding tables. Fresh re-judges.
# --------------------------------------------------------------------------- #

def _judge_env(env, monkeypatch, calls):
    runner, ai_review = env["runner"], env["ai_review"]
    monkeypatch.setattr(runner, "_extraction_for", lambda o, pages=None: _extraction())
    monkeypatch.setattr(runner, "_content_hash_of", lambda oid: "H")   # stable content
    monkeypatch.setattr(ai_review, "cross_output_judge", lambda bundle, c: [])

    def judge(ex, pri, label, title, cfg):
        calls.append(label)
        return [{"check_id": "AIW-2.1", "message": "m", "severity": "high",
                 "risk": "High", "numbers": [], "subjects": []}]
    monkeypatch.setattr(ai_review, "within_table_judge", judge)

    def go(kind="incremental"):
        rid = _mk_run(env)
        runner.RUN_PROGRESS[env["pid"]] = {"running": True, "done": 0,
                                           "total": len(env["targets"]), "message": "",
                                           "run_id": rid, "skipped": 0, "errors": []}
        runner._do_run(env["pid"], rid, kind, env["current"], None, env["targets"])
        return runner.RUN_PROGRESS[env["pid"]]["summary"]
    return go


def test_incremental_rerun_skips_already_judged_tables(env, monkeypatch):
    calls = []
    go = _judge_env(env, monkeypatch, calls)
    s1 = go(); n = len(env["targets"])
    assert len(calls) == n and s1["findings"] == n          # cold: judged all
    calls.clear()
    s2 = go()                                               # unchanged re-run
    assert calls == [] and s2["findings"] == n              # judged none, findings kept


def test_resume_after_crash_only_finishes_outstanding_tables(env, monkeypatch):
    calls = []
    go = _judge_env(env, monkeypatch, calls)
    go()                                                    # full run
    calls.clear()
    db = env["db"]
    # Emulate a crash that left ONE table unfinished: its judge_key was never stamped and its finding not written.
    victim = env["targets"][2]["id"]
    db.execute("UPDATE output SET judge_key=NULL WHERE id=?", (victim,))
    db.execute("DELETE FROM finding WHERE output_id=? AND phase='within'", (victim,))
    s = go()
    assert calls == ["Table 3"], f"resume re-judged {calls}, expected only Table 3"
    assert s["findings"] == len(env["targets"])             # back to complete


def test_fresh_rejudges_everything(env, monkeypatch):
    calls = []
    go = _judge_env(env, monkeypatch, calls)
    go(); calls.clear()
    go("fresh")
    assert len(calls) == len(env["targets"])                # fresh cleared judge_key


def test_imported_findings_survive_a_run(env, monkeypatch):
    """Excel-imported findings (phase 'imported') must not be wiped by an AI run."""
    import export
    db = env["db"]
    import io
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active
    ws.append(["Output", "Risk", "Finding"])
    ws.append(["Table 1", "High", "Imported: reviewer-authored note"])
    buf = io.BytesIO(); wb.save(buf)
    summ = export.import_findings_xlsx(env["pid"], buf.getvalue())
    assert summ["imported"] == 1

    calls = []
    go = _judge_env(env, monkeypatch, calls)
    go()   # a full AI run
    imported = db.query("SELECT * FROM finding WHERE project_id=? AND phase='imported'",
                        (env["pid"],))
    assert len(imported) == 1, "AI run wiped the imported finding"


# --------------------------------------------------------------------------- #
# Transient page-slice failures: retried, and if they finally fail the page is reported (not silently dropped) AND the extraction is not cached as complete, so a plain re-run retries it.
# --------------------------------------------------------------------------- #

def test_slice_retry_recovers_a_transient_failure(monkeypatch):
    import ai_review
    monkeypatch.setattr(ai_review, "_MAX_SLICES", 100)
    monkeypatch.setattr(ai_review, "_SLICE_RETRIES", 2)
    tries = {"p2": 0}

    def slice_fn(text, label, groups, page):
        if page == 2:
            tries["p2"] += 1
            if tries["p2"] < 2:
                raise RuntimeError("overloaded_error: slow down")
        return {"groups": [], "summary_rows": [{"label": f"r{page}", "values": {}, "page": page}],
                "footnote_markers": [], "pt_terms": [], "missing_n_rows": [], "notes": "",
                "analysis_set": "", "run_datetime": "", "header_label": label,
                "is_ae_pt_table": False}
    monkeypatch.setattr(ai_review, "_extract_slice", slice_fn)
    ex = ai_review._extract_paged(["x" * 900 for _ in range(4)], "T")
    assert tries["p2"] == 2                         # retried once
    assert ex["coverage"]["pages_read"] == 4
    assert not ex["coverage"].get("read_errors")


def test_persistent_slice_failure_is_recorded_and_not_baked_into_cache(env, monkeypatch):
    import ai_review, pdftools
    runner, db = env["runner"], env["db"]
    monkeypatch.setattr(ai_review, "_MAX_SLICES", 100)
    monkeypatch.setattr(ai_review, "_SLICE_RETRIES", 1)

    def slice_fn(text, label, groups, page):
        if page == 3:
            raise RuntimeError("bad_request: unparseable")
        return {"groups": [], "summary_rows": [{"label": f"r{page}", "values": {}, "page": page}],
                "footnote_markers": [], "pt_terms": [], "missing_n_rows": [], "notes": "",
                "analysis_set": "", "run_datetime": "", "header_label": label,
                "is_ae_pt_table": False}
    monkeypatch.setattr(ai_review, "_extract_slice", slice_fn)
    monkeypatch.setattr(pdftools, "content_hash", lambda t: "H")
    monkeypatch.setattr(pdftools, "file_hash", lambda p: "F")
    # Pages long enough (each > _CHUNK_CHARS) that extract() takes the PAGED path.
    monkeypatch.setattr(pdftools, "page_texts", lambda path, a, b: ["x" * 5000 for _ in range(a, b + 1)])

    o = dict(env["targets"][0]); o["page_start"], o["page_end"] = 1, 4   # 4-page table
    ex = runner._extraction_for(o)
    assert ex["coverage"]["read_errors"]                     # failure captured
    row = db.one("SELECT content_hash, src_hash FROM output WHERE id=?", (o["id"],))
    assert row["content_hash"] == "H"                        # judging can still use it
    assert row["src_hash"] is None                           # but re-run WILL re-read/retry


def test_content_hash_cache_does_not_serve_a_failed_partial(env, monkeypatch):
    """The regression that made 'nothing fixed' persist: a partial extraction whose content_hash still matches (PDF text unchanged) was served by _extraction_for every run, so the failed pages were never re-attempted. It must now RE-EXTRACT instead."""
    import ai_review, pdftools
    runner, db = env["runner"], env["db"]
    monkeypatch.setattr(ai_review, "_MAX_SLICES", 100)
    monkeypatch.setattr(pdftools, "file_hash", lambda p: "FH")
    pages = ["p%d " % i + "z" * 5000 for i in range(1, 6)]      # 5 pages, forces paged path
    monkeypatch.setattr(pdftools, "page_texts", lambda path, a, b: pages)
    monkeypatch.setattr(pdftools, "content_hash", lambda t: "CH")   # stable across runs

    o = dict(env["targets"][0]); o["page_start"], o["page_end"] = 1, 5
    import json as _j
    partial = {"summary_rows": [], "coverage": {"pages_total": 5, "pages_read": 3,
                                                 "truncated": False, "incomplete": True}}
    db.execute("UPDATE output SET extraction_json=?, content_hash='CH', src_hash='FH' WHERE id=?",
               (_j.dumps(partial), o["id"]))
    # neither cache layer may serve the failed partial
    assert runner._cached_extraction(o) is None

    calls = {"n": 0}

    def good_slice(text, label, groups, page):
        calls["n"] += 1
        return {"groups": [], "summary_rows": [{"label": f"r{page}", "values": {}, "page": page}],
                "footnote_markers": [], "pt_terms": [], "missing_n_rows": [], "notes": "",
                "analysis_set": "", "run_datetime": "", "header_label": label, "is_ae_pt_table": False}
    monkeypatch.setattr(ai_review, "_extract_slice", good_slice)
    ex = runner._extraction_for(o)
    assert calls["n"] >= 5, "did not re-extract — the partial was served again"
    assert ex["coverage"]["pages_read"] == 5     # recovered
    assert db.one("SELECT src_hash FROM output WHERE id=?", (o["id"],))["src_hash"] == "FH"


# --------------------------------------------------------------------------- #
# Fail-closed completion and run isolation.  A valid empty finding list is a clean result; an exception, malformed/missing comparison, failed cross phase, or incomplete extraction is not.
# --------------------------------------------------------------------------- #

def _run_direct(env):
    runner = env["runner"]
    current, prior = runner._pick_current_prior(env["pid"])
    rid = _mk_run(env)
    runner.RUN_PROGRESS[env["pid"]] = {
        "running": True, "done": 0, "total": len(env["targets"]),
        "message": "", "run_id": rid, "skipped": 0, "errors": [],
    }
    runner._do_run(env["pid"], rid, "incremental", current, prior,
                   env["targets"])
    return runner.RUN_PROGRESS[env["pid"]]["summary"]


def test_all_nonconnection_judge_failures_never_autoapprove(env, monkeypatch):
    runner, db = env["runner"], env["db"]
    monkeypatch.setattr(runner, "_extraction_for", lambda o, pages=None: _extraction())
    monkeypatch.setattr(env["ai_review"], "within_table_judge",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("429 rate limit")))

    summary = _run_direct(env)
    assert summary["status"] == "failed"
    assert summary["review_complete"] is False
    assert summary["n_judge_failed"] == len(env["targets"])
    assert summary["auto_approved"] == 0
    assert {r["status"] for r in db.query("SELECT status FROM output WHERE project_id=?",
                                           (env["pid"],))} == {"Not Reviewed"}


def test_cross_judge_failure_preserves_previous_cross_snapshot(env, monkeypatch):
    runner, db = env["runner"], env["db"]
    monkeypatch.setattr(runner, "_extraction_for", lambda o, pages=None: _extraction())
    monkeypatch.setattr(env["ai_review"], "within_table_judge", lambda *a, **k: [])
    old_id = db.insert(
        "finding", project_id=env["pid"], output_id=None, run_id=None,
        check_id="AIX-OLD", severity="high", risk="High", message="last complete snapshot",
        subjects="[]", numbers="[]", signature="old", state="pending", badge="",
        phase="cross", affected="[]",
    )
    monkeypatch.setattr(
        env["ai_review"], "cross_output_judge",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("judge overloaded")),
    )

    summary = _run_direct(env)
    assert summary["status"] == "partial"
    assert summary["review_complete"] is False
    assert summary["auto_approved"] == 0
    assert db.one("SELECT id FROM finding WHERE id=?", (old_id,)) is not None


def test_missing_required_prior_extraction_blocks_clean_completion(env, monkeypatch):
    runner, db = env["runner"], env["db"]
    prior_doc = db.insert("document", project_id=env["pid"], role="prior",
                          filename="prior.pdf", path=str(env["tmp"] / "prior.pdf"),
                          n_pages=1, edition="2024")
    db.insert("output", project_id=env["pid"], document_id=prior_doc, seq=1,
              output_type="Table", number="1", label="Table 1", title="old",
              page_start=1, page_end=1, status="Not Reviewed")

    def extraction(o, pages=None):
        return None if o["document_id"] == prior_doc else _extraction()

    monkeypatch.setattr(runner, "_extraction_for", extraction)
    monkeypatch.setattr(env["ai_review"], "within_table_judge", lambda *a, **k: [])
    summary = _run_direct(env)
    assert summary["review_complete"] is False
    assert summary["auto_approved"] == 0
    assert any("prior-edition extraction failed" in e for e in summary["errors"])


def test_partial_rerun_demotes_only_stale_system_approvals(env, monkeypatch):
    runner, db = env["runner"], env["db"]
    db.execute("UPDATE output SET status='Auto-approved' WHERE project_id=?", (env["pid"],))
    db.execute("UPDATE output SET status='In Progress' WHERE id=?", (env["targets"][1]["id"],))
    monkeypatch.setattr(runner, "_extraction_for", lambda o, pages=None: _extraction())

    def judge(ex, prior, label, title, cfg):
        if label == "Table 3":
            raise RuntimeError("invalid structured output")
        return []

    monkeypatch.setattr(env["ai_review"], "within_table_judge", judge)
    summary = _run_direct(env)
    statuses = {r["label"]: r["status"] for r in
                db.query("SELECT label, status FROM output WHERE project_id=?", (env["pid"],))}
    assert summary["review_complete"] is False
    assert statuses["Table 2"] == "In Progress"       # human state preserved
    assert all(statuses[label] == "Not Reviewed"
               for label in env["labels"] if label != "Table 2")


def test_cross_finding_blocks_every_affected_output_from_autoapproval(env, monkeypatch):
    runner, db = env["runner"], env["db"]
    monkeypatch.setattr(runner, "_extraction_for", lambda o, pages=None: _extraction())
    monkeypatch.setattr(env["ai_review"], "within_table_judge", lambda *a, **k: [])
    monkeypatch.setattr(env["ai_review"], "cross_output_judge", lambda *a, **k: [{
        "check_id": "AIX-3", "message": "cross mismatch", "severity": "high",
        "risk": "High", "numbers": [], "subjects": [],
        "affected": ["Table 1", "Table 2"],
    }])

    summary = _run_direct(env)
    statuses = {r["label"]: r["status"] for r in
                db.query("SELECT label, status FROM output WHERE project_id=?", (env["pid"],))}
    assert summary["review_complete"] is True
    assert statuses["Table 1"] == statuses["Table 2"] == "Not Reviewed"
    assert all(statuses[label] == "Auto-approved" for label in env["labels"][2:])


def test_project_lease_is_atomic_and_blocks_single_output_run(env, monkeypatch):
    runner = env["runner"]
    configure_calls = []
    original_configure = runner.ai_client.configure
    monkeypatch.setattr(
        runner.ai_client, "configure",
        lambda *a, **k: (configure_calls.append((a, k)), original_configure(*a, **k))[1],
    )
    token = runner._acquire_project_lease(env["pid"])
    try:
        with pytest.raises(runner.RunAlreadyActive):
            runner.run_single_output(env["targets"][0]["id"])
        assert configure_calls == [], "a rejected run mutated the active run's model/effort"
        # The lease is process-global because model/effort configuration is also process-scoped; a different project cannot bleed its config into this run.
        with pytest.raises(runner.RunAlreadyActive):
            runner._acquire_project_lease(env["pid"] + 999)
    finally:
        runner._release_project_lease(env["pid"], token)

    # Under simultaneous acquisition, exactly one contender can own the project.
    barrier = threading.Barrier(8)
    tokens = []
    lock = threading.Lock()

    def contender():
        barrier.wait()
        try:
            t = runner._acquire_project_lease(env["pid"])
        except runner.RunAlreadyActive:
            return False
        with lock:
            tokens.append(t)
        return True

    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        won = list(pool.map(lambda _: contender(), range(8)))
    assert sum(won) == 1
    runner._release_project_lease(env["pid"], tokens[0])
    assert not runner.project_run_active(env["pid"])


def test_single_output_failure_releases_project_lease(env, monkeypatch):
    runner = env["runner"]
    monkeypatch.setattr(runner, "_page_texts_for",
                        lambda o: (_ for _ in ()).throw(RuntimeError("PDF unreadable")))
    result = runner.run_single_output(env["targets"][0]["id"])
    assert result["ok"] is False and "PDF unreadable" in result["error"]
    assert not runner.project_run_active(env["pid"])


def test_background_thread_start_failure_releases_lease_and_finishes_run(env, monkeypatch):
    runner, db = env["runner"], env["db"]

    class BrokenThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("thread could not start")

    monkeypatch.setattr(runner.threading, "Thread", BrokenThread)

    with pytest.raises(RuntimeError, match="thread could not start"):
        runner.start_run(env["pid"])

    assert not runner.project_run_active(env["pid"])
    row = db.one("SELECT finished_at, summary_json FROM ai_run ORDER BY id DESC LIMIT 1")
    summary = db.loads(row["summary_json"], {})
    assert row["finished_at"] is not None
    assert summary["status"] == "failed"
    assert summary["review_complete"] is False
    assert summary["errors"] == ["thread could not start"]
