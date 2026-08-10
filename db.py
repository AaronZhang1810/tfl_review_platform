"""SQLite persistence for the TLF Review Platform.

Single-user: no auth / roles / per-user isolation. One SQLite file holds projects,
their outputs (one per TLF table), reviewer comments, AI findings, annotations, and
AI-run bookkeeping.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

DATA_DIR = os.path.abspath(os.environ.get(
    "TLF_DATA_DIR", os.path.join(os.path.dirname(__file__), "data")
))
DB_PATH = os.path.join(DATA_DIR, "app.db")

_local = threading.local()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get() -> sqlite3.Connection:
    """One connection per thread (FastAPI runs handlers across a threadpool, and an AI
    run fans its per-table work out over a thread pool of its own).

    WAL + busy_timeout matter for that second case: several extraction workers finish at
    once and each writes its own extraction_json. Under the default rollback journal a
    writer blocks readers and a 5 s timeout is easy to exceed, which surfaces as
    "database is locked". WAL lets one writer proceed alongside readers, and the timeout
    makes a contended write wait its turn instead of failing.
    """
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")   # persists on the DB file
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA synchronous = NORMAL")  # standard durable pairing for WAL
        _local.conn = conn
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS project (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    compound TEXT, study TEXT, name TEXT,
    edition_label TEXT,           -- e.g. "Annual edition (2025)"
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS document (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER REFERENCES project(id) ON DELETE CASCADE,
    role TEXT,                    -- 'delivery' | 'sap' | 'protocol' | 'prior'
    filename TEXT, path TEXT, n_pages INTEGER,
    edition TEXT                  -- e.g. "2025" (detected from the cover/header)
);
CREATE TABLE IF NOT EXISTS output (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER REFERENCES project(id) ON DELETE CASCADE,
    document_id INTEGER REFERENCES document(id) ON DELETE CASCADE,
    seq INTEGER,                  -- order within the document
    output_type TEXT,             -- Table | Listing | Figure
    number TEXT,                  -- e.g. "1", "4.2.1"
    label TEXT,                   -- e.g. "Table 1"
    title TEXT,
    page_start INTEGER, page_end INTEGER,  -- 1-based inclusive
    status TEXT DEFAULT 'Not Reviewed',
    extraction_json TEXT,         -- cached AI extraction
    content_hash TEXT             -- hash of clipped text (extraction cache key)
);
CREATE TABLE IF NOT EXISTS comment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER REFERENCES project(id) ON DELETE CASCADE,
    output_id INTEGER REFERENCES output(id) ON DELETE CASCADE,
    title TEXT, body TEXT,
    source TEXT DEFAULT 'manual', -- 'manual' | 'ai' | 'annotation'
    finding_id INTEGER,
    resolved INTEGER DEFAULT 0,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS finding (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER REFERENCES project(id) ON DELETE CASCADE,
    output_id INTEGER REFERENCES output(id) ON DELETE CASCADE,
    run_id INTEGER,
    check_id TEXT, severity TEXT, message TEXT,
    risk TEXT,                     -- High | Medium | Low (checklist risk; severity is derived)
    subjects TEXT, numbers TEXT, page INTEGER,
    printed_page INTEGER,          -- printed subtable page, e.g. 2 of "TABLE PAGE 2 of 10"
    pages_total INTEGER,           -- total printed pages in the table, e.g. 10
    section TEXT,                  -- nearest indication/category header (e.g. 'Condition Alpha')
    row_kind TEXT,                 -- 'study' (per-subject row) | 'aggregate' (subtotal/category)
    signature TEXT,
    state TEXT DEFAULT 'pending',  -- pending | posted | rejected | resolved
    badge TEXT DEFAULT '',         -- '' | 'new' | 'potentially_resolved'
    affected TEXT                  -- JSON list of output labels (XOUT findings)
);
CREATE TABLE IF NOT EXISTS annotation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    output_id INTEGER REFERENCES output(id) ON DELETE CASCADE,
    kind TEXT,                    -- highlight | rect | freehand
    page INTEGER,                 -- 1-based within the clipped output
    geom_json TEXT, note TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS ai_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER REFERENCES project(id) ON DELETE CASCADE,
    kind TEXT, started_at TEXT, finished_at TEXT,
    summary_json TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, actor TEXT,
    action TEXT,                  -- e.g. 'status.set', 'comment.add', 'finding.post'
    project_id INTEGER, entity TEXT, entity_id INTEGER,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS review_log (
    -- One row per HUMAN review decision, captured with full context so a FUTURE
    -- step can learn deterministic rules from how reviewers act (capture only —
    -- nothing is derived from this table yet).
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    reviewer TEXT,
    action TEXT,                  -- post | reject | reopen | comment_add | comment_resolve
    project_id INTEGER,
    output_label TEXT,
    check_id TEXT,
    checklist_item TEXT,          -- e.g. '2.1' (derived from check_id)
    risk TEXT,
    message TEXT,
    numbers TEXT,                 -- JSON list
    subjects TEXT,                -- JSON list
    page INTEGER,
    printed_page INTEGER,
    section TEXT,
    comment_text TEXT,
    finding_signature TEXT
);
"""


def init() -> None:
    conn = get()
    conn.executescript(SCHEMA)
    # Lightweight migrations (ADD COLUMN is a no-op error if already present).
    for stmt in (
        "ALTER TABLE comment ADD COLUMN author TEXT",
        "ALTER TABLE comment ADD COLUMN parent_id INTEGER",
        # Per-Table sequential comment number (1,2,3,…) — the ID a reviewer sees and the
        # (Table, num) key that comment import/export round-trips on. Backfilled below.
        "ALTER TABLE comment ADD COLUMN num INTEGER",
        "ALTER TABLE finding ADD COLUMN printed_page INTEGER",
        "ALTER TABLE finding ADD COLUMN pages_total INTEGER",
        "ALTER TABLE finding ADD COLUMN section TEXT",
        "ALTER TABLE finding ADD COLUMN row_kind TEXT",
        "ALTER TABLE finding ADD COLUMN risk TEXT",
        # Byte hash of the source PDF when this output's extraction was stored. If the
        # file still hashes the same, the page text cannot have changed, so a run can
        # reuse the extraction WITHOUT re-reading the PDF (pdfplumber is ~0.6 s/page —
        # ~18 min per run on a two-edition delivery that has not changed at all).
        "ALTER TABLE output ADD COLUMN src_hash TEXT",
        # Fingerprint of what this output's WITHIN-TABLE findings depend on (its
        # extraction, the prior edition's, the model, effort and checklist). If it still
        # matches, the findings are current, so a re-run — or a run resumed after a crash
        # — skips re-judging this table and keeps them. This is what makes "Run AI review"
        # continuable: completed tables are durable and are not redone.
        "ALTER TABLE output ADD COLUMN judge_key TEXT",
        # Which phase produced a finding: 'within' (per-table judge), 'cross'
        # (cross-output), 'structural' (deterministic) or 'imported' (from Excel). Lets a
        # run replace exactly one phase's findings without disturbing the others — e.g.
        # re-judge one table, or re-import, without wiping cross-output or AI findings.
        "ALTER TABLE finding ADD COLUMN phase TEXT",
    ):
        try:
            conn.execute(stmt)
        except Exception:
            pass
    # Review status was split into human ('Manually approved') vs AI ('Auto-approved'). Legacy
    # rows only ever stored 'Approved'; reclassify them the way a fresh run now would — a table
    # with no finding against it was a clean AI pass → 'Auto-approved'; one that still carries a
    # finding was a human sign-off despite it → 'Manually approved'. Order matters (clear the
    # clean rows first). Idempotent: a no-op once no 'Approved' rows remain.
    conn.execute("UPDATE output SET status='Auto-approved' WHERE status='Approved' "
                 "AND id NOT IN (SELECT output_id FROM finding WHERE output_id IS NOT NULL)")
    conn.execute("UPDATE output SET status='Manually approved' WHERE status='Approved'")
    # Backfill `num` for any comment that predates the column: number each output's comments
    # in creation order so every existing comment gets a stable per-Table ID. Idempotent —
    # once every row is numbered the scan returns nothing.
    unnumbered = conn.execute(
        "SELECT id, output_id FROM comment WHERE num IS NULL ORDER BY output_id, created_at, id"
    ).fetchall()
    counters: dict = {}
    for r in unnumbered:
        oid = r["output_id"]
        if oid not in counters:
            counters[oid] = conn.execute(
                "SELECT COALESCE(MAX(num), 0) FROM comment WHERE output_id IS ?", (oid,)
            ).fetchone()[0]
        counters[oid] += 1
        conn.execute("UPDATE comment SET num=? WHERE id=?", (counters[oid], r["id"]))
    conn.commit()


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def insert(table: str, **cols: Any) -> int:
    keys = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    conn = get()
    cur = conn.execute(f"INSERT INTO {table} ({keys}) VALUES ({marks})", tuple(cols.values()))
    conn.commit()
    return cur.lastrowid


def query(sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in get().execute(sql, params).fetchall()]


def one(sql: str, params: tuple = ()) -> dict | None:
    row = get().execute(sql, params).fetchone()
    return dict(row) if row else None


def execute(sql: str, params: tuple = ()) -> None:
    conn = get()
    conn.execute(sql, params)
    conn.commit()


def audit(actor: str, action: str, entity: str, entity_id, project_id=None, detail: str = "") -> None:
    """Append an immutable audit record (who / what / when) — ALCOA+ traceability."""
    insert("audit_log", ts=now_iso(), actor=actor or "system", action=action,
           project_id=project_id, entity=entity, entity_id=entity_id, detail=detail)


# --------------------------------------------------------------------------- #
# Human-review learning log (capture only; rule derivation is a FUTURE step)
# --------------------------------------------------------------------------- #

# check_id -> checklist item id for the structural checks and the missing-N note.
# AI-judge findings carry their prefix (AIW-/AIX-/AIV-) + item id, stripped below.
_CHECK_TO_ITEM = {"FMT-010": "1.1", "XOUT-020": "1.2", "XOUT-001": "1.3", "FMT-002": "2"}


def checklist_item_for(check_id: str) -> str:
    """Map a finding's check_id back to its checklist item id (e.g. 'AIW-2.1' -> '2.1',
    'FMT-010' -> '1.1'). Returns '' when there is no mapping."""
    if not check_id:
        return ""
    if check_id in _CHECK_TO_ITEM:
        return _CHECK_TO_ITEM[check_id]
    for pref in ("AIW-", "AIX-", "AIV-"):
        if check_id.startswith(pref):
            return check_id[len(pref):]
    return ""


def log_finding_action(reviewer: str, action: str, finding_row: dict | None, comment_text: str = "") -> None:
    """Record a human decision on a FINDING (post/reject/reopen) with its full context.
    `finding_row` is the finding as stored (numbers/subjects are already JSON strings)."""
    if not finding_row:
        return
    out_label = ""
    if finding_row.get("output_id"):
        o = one("SELECT label FROM output WHERE id=?", (finding_row["output_id"],))
        out_label = (o or {}).get("label") or ""
    if not out_label:
        aff = loads(finding_row.get("affected"), [])
        if aff:
            out_label = aff[0]
    cid = finding_row.get("check_id", "") or ""
    insert("review_log", ts=now_iso(), reviewer=reviewer or "system", action=action,
           project_id=finding_row.get("project_id"), output_label=out_label,
           check_id=cid, checklist_item=checklist_item_for(cid),
           risk=finding_row.get("risk") or "", message=finding_row.get("message") or "",
           numbers=finding_row.get("numbers") or "[]",
           subjects=finding_row.get("subjects") or "[]",
           page=finding_row.get("page"), printed_page=finding_row.get("printed_page"),
           section=finding_row.get("section"),
           comment_text=comment_text, finding_signature=finding_row.get("signature") or "")


def log_comment_action(reviewer: str, action: str, project_id, output_label: str = "",
                       comment_text: str = "") -> None:
    """Record a human COMMENT action (comment_add / comment_resolve) not tied to a
    specific AI finding."""
    insert("review_log", ts=now_iso(), reviewer=reviewer or "system", action=action,
           project_id=project_id, output_label=output_label or "",
           check_id="", checklist_item="", risk="", message="",
           numbers="[]", subjects="[]", page=None, printed_page=None, section=None,
           comment_text=comment_text, finding_signature="")


def recover_stale_runs() -> int:
    """Mark AI runs that were left unfinished (e.g. by a server restart/crash) as
    interrupted. Returns how many were reconciled."""
    conn = get()
    cur = conn.execute(
        "UPDATE ai_run SET finished_at=?, summary_json=? WHERE finished_at IS NULL",
        (now_iso(), json.dumps({"error": "interrupted (server restart)"})),
    )
    conn.commit()
    return cur.rowcount or 0


def loads(v: str | None, default=None):
    if not v:
        return default
    try:
        return json.loads(v)
    except Exception:
        return default
