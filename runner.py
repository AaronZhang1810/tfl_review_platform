"""AI-review run orchestration.

Reviews every table in the current edition. For each table: extract (cached by content hash) → self-check the extraction (Precision guard #1) → the within-table AI judge (checklist 2.x / 7.2, plus the version items 6.x / 7.3 / 7.4 folded in when a prior edition exists) → verify pass (Precision guard #2) → dedupe. After the per-table loop, one cross-output AI judge reconciles numbers across the whole delivery (checklist 3 / 4 / 5 / 7.1 / 8). Finally the deterministic STRUCTURAL checks run (blank pages 1.1, missing-vs-prior 1.2, numbering gap 1.3). Runs in a background thread; progress is polled via RUN_PROGRESS."""

from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import os
import re
import threading
from datetime import datetime

import db
import pdftools
import ai_review
import ai_client
import checks

# Every table in the current edition is reviewed. This ceiling is only a runaway guard for pathological deliveries; real TLF sets are far below it. Override with TLF_MAX_TARGETS if ever needed.
MAX_CURRENT_TARGETS = int(os.environ.get("TLF_MAX_TARGETS", "1000"))

RUN_PROGRESS: dict[int, dict] = {}
_YEAR_RE = re.compile(r"\b(20\d{2})\b")

# Guards RUN_PROGRESS mutation: the per-table pool increments done/conn_errors from several threads, and "read, add one, store" is not atomic.
_prog_lock = threading.Lock()

# Review configuration is process-scoped in the current client, while each project also has one mutable review snapshot. Letting any two full/single reviews overlap can therefore mix model/effort configuration even when they target different projects, and can corrupt findings when they target the same one. This deliberately process-global lease serializes mutating review runs until all AI call sites take an explicit immutable config.
_lease_lock = threading.Lock()
_project_leases: dict[int, object] = {}
_config_lock = threading.Lock()


class RunAlreadyActive(RuntimeError):
    """Raised when another mutating AI review already owns this project."""


def _acquire_project_lease(pid: int) -> object:
    token = object()
    with _lease_lock:
        if _project_leases:
            active_pid = next(iter(_project_leases))
            raise RunAlreadyActive(
                f"an AI review is already running for project {active_pid}; "
                "wait for it to finish before starting another"
            )
        _project_leases[pid] = token
    return token


def _release_project_lease(pid: int, token: object | None) -> None:
    if token is None:
        return
    with _lease_lock:
        if _project_leases.get(pid) is token:
            _project_leases.pop(pid, None)


def project_run_active(pid: int) -> bool:
    with _lease_lock:
        return pid in _project_leases


def _capture_run_config(model: str | None, effort: str | None) -> dict:
    """Resolve one immutable model/effort snapshot before a worker is spawned.

configure()+run_config() must be atomic while compatibility globals still exist in ai_client.  The returned dict, rather than those globals, is passed into every review worker.  ai_client's per-context configuration then keeps calls from different projects isolated."""
    with _config_lock:
        ai_client.configure(model=model, effort=effort)
        return dict(ai_client.run_config())


def _norm_label(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


# Bump when the JUDGING logic itself changes in a way that should invalidate cached per-table judgements even though extraction/model/effort/checklist are unchanged.
_JUDGE_LOGIC_VERSION = "2"


def _checklist_sig(config: dict) -> str:
    """Stable hash of the config parts that change judgements. Editing the checklist (or a precision-guard flag) in study_config.json changes this, so cached judgements for every table are invalidated and re-judged on the next run."""
    keep = {k: config.get(k) for k in
            ("checklist", "selfcheck", "verify_pass", "dedupe_findings",
             "structural_checks", "deterministic_checks")}
    return hashlib.blake2b(json.dumps(keep, sort_keys=True, ensure_ascii=False).encode("utf-8"),
                           digest_size=8).hexdigest()


def _judge_key(cur_hash, prior_hash, model, effort, checklist_sig) -> str:
    """Fingerprint of everything a table's within-table findings depend on. Stored on the output after judging; when it still matches, the findings are current and re-judging is skipped — which is what makes a run resumable after a crash and fast to re-run."""
    parts = "|".join([_JUDGE_LOGIC_VERSION, cur_hash or "", prior_hash or "",
                      model or "", effort or "", checklist_sig or ""])
    return hashlib.blake2b(parts.encode("utf-8"), digest_size=12).hexdigest()


def _content_hash_of(output_id: int) -> str | None:
    row = db.one("SELECT content_hash FROM output WHERE id=?", (output_id,))
    return row["content_hash"] if row else None


def _incomplete_by_failure(ex: dict | None) -> bool:
    """True if this stored extraction is missing pages because slice reads FAILED (not because the slice cap trimmed them). Such an extraction must never be served from any cache — it has to be re-read so the failed pages are retried. A cap-truncation is as complete as it will get, so it is NOT flagged here."""
    cov = (ex or {}).get("coverage") or {}
    t, r = cov.get("pages_total"), cov.get("pages_read")
    shortfall = isinstance(t, int) and isinstance(r, int) and r < t
    return bool(cov.get("read_errors") or shortfall) and not cov.get("truncated")


def detect_edition(path: str) -> str:
    """Best-effort edition year from the first page header."""
    try:
        txt = pdftools.range_text(path, 1, 1, max_chars=3000)
    except Exception:
        return ""
    years = _YEAR_RE.findall(txt)
    return max(years) if years else ""


def _delivery_docs(pid: int) -> list[dict]:
    return db.query("SELECT * FROM document WHERE project_id=? AND role='delivery'", (pid,))


def _pick_current_prior(pid: int):
    """The 'current' document is the one under review; 'prior' is the comparison edition.

When the user marked a main document at creation, exactly one delivery doc keeps role='delivery' and the rest are role='prior' — the marked one is current, the highest-edition prior is the comparison. With no explicit pick (all role='delivery', e.g. a single-file project), the highest edition wins, as before."""
    docs = db.query(
        "SELECT * FROM document WHERE project_id=? AND role IN ('delivery','prior')", (pid,))
    if not docs:
        return None, None
    by_edition = lambda d: (d.get("edition") or "")
    mains = sorted((d for d in docs if d["role"] == "delivery"), key=by_edition, reverse=True)
    priors = sorted((d for d in docs if d["role"] == "prior"), key=by_edition, reverse=True)
    if priors:                      # explicit main chosen at creation
        current = mains[0] if mains else None
        return current, priors[0]
    current = mains[0]              # legacy: no explicit pick → highest edition is current
    prior = mains[1] if len(mains) > 1 else None
    return current, prior


def _doc_path(output: dict) -> str:
    doc = db.one("SELECT * FROM document WHERE id=?", (output["document_id"],))
    return doc["path"]


def _page_texts_for(output: dict) -> list[str]:
    """Per-page text for one output. Extraction is the single most expensive non-API step in a run (pdfplumber is ~0.6 s/page), so callers pass the result around rather than re-deriving it — see _analyze_table."""
    return pdftools.page_texts(_doc_path(output), output["page_start"], output["page_end"])


def _cached_extraction(output: dict) -> dict | None:
    """Cache hit that costs NO PDF read.

If the source file still hashes to what was stored with this extraction, the file is byte-identical, so its page text cannot have changed and the extraction is still valid. That lets a re-run skip pdfplumber entirely — on an unchanged two-edition delivery that is ~18 min of CPU per run spent only to re-derive a cache key.

Returns None whenever anything is unproven (no stored hash, file changed, file unreadable), and the caller falls back to the full text-hash path."""
    row = db.one("SELECT extraction_json, src_hash FROM output WHERE id=?", (output["id"],))
    if not row or not row.get("extraction_json") or not row.get("src_hash"):
        return None
    try:
        if pdftools.file_hash(_doc_path(output)) != row["src_hash"]:
            return None
    except OSError:
        return None                     # file moved/unreadable → prove it the slow way
    ex = db.loads(row["extraction_json"], {})
    # Never serve an extraction left INCOMPLETE by failed page-reads: returning None forces a re-read + slice-retry, and self-heals partial extractions cached by older code (which set src_hash even on a partial read).
    if _incomplete_by_failure(ex):
        return None
    return ex


def _extraction_for(output: dict, pages: list[str] | None = None) -> dict | None:
    """Return cached extraction or compute it, storing text hash + json.

`pages` lets a caller hand in text it already extracted, so one table's text is read from the PDF once per run instead of once here and again for the self-check."""
    if pages is None:
        pages = _page_texts_for(output)
    full = "\n".join(pages)
    # Hash the FULL text, not a truncated prefix: the hash is the cache-invalidation key, and a 91-page table is ~287k chars, so hashing only the first 60k left ~80% of the table outside the key — a number edited on a later page would not invalidate the cached extraction. The PROMPT stays capped (below); only the key is full-fidelity.
    h = pdftools.content_hash(full)
    text = full[:60000]
    # Read the cache columns from the DB, not the passed dict: a "fresh" run and the per-output re-run NULL these in the DB right before calling us, but the output dict was fetched earlier and still holds the old values — trusting it would wrongly hit the cache and skip re-extraction on unchanged content.
    cached = db.one("SELECT extraction_json, content_hash FROM output WHERE id=?", (output["id"],))
    try:
        fh = pdftools.file_hash(_doc_path(output))
    except OSError:
        fh = None
    if cached and cached.get("extraction_json") and cached.get("content_hash") == h:
        prev = db.loads(cached["extraction_json"], {})
        # The text is unchanged, so a COMPLETE cached extraction still stands — reuse it and stamp src_hash so the next run can skip the PDF read entirely. But if the cached one is incomplete because slices FAILED, do NOT reuse it: fall through and re-extract so those pages are retried. (This is the layer that was silently serving the partial every run — content_hash always matches when the PDF hasn't changed, so without this check the failed pages were never re-attempted.)
        if not _incomplete_by_failure(prev):
            if fh:
                db.execute("UPDATE output SET src_hash=? WHERE id=?", (fh, output["id"]))
            return prev
    try:
        ex = ai_review.extract(text, output["label"], page_texts=pages)
    except Exception as e:
        with _prog_lock:      # called from the per-table pool → guard the shared dict
            prog = RUN_PROGRESS.get(output["project_id"], {})
            prog.setdefault("errors", []).append(f"{output['label']}: {e}")
            if ai_client.is_connection_error(e):
                prog["conn_errors"] = prog.get("conn_errors", 0) + 1
        return None
    # A PARTIAL extraction (some page-slices failed to read) is stored so judging can use what we got, but WITHOUT src_hash — that withholds the no-PDF-read fast path so a plain re-run re-reads the file and retries the failed pages instead of reusing the gap.
    partial = bool((ex.get("coverage") or {}).get("read_errors"))
    db.execute("UPDATE output SET extraction_json=?, content_hash=?, src_hash=? WHERE id=?",
               (json.dumps(ex), h, (None if partial else fh), output["id"]))
    return ex


def _analyze_table(o: dict, config: dict, prior_by_label: dict, cfg: dict,
                   run_id: int, stop: threading.Event) -> dict:
    """Analyze ONE table. Runs in a worker thread of the per-table pool.

Persists this table's within-table findings itself (delete-then-insert under phase 'within', then stamps output.judge_key) so a completed table is DURABLE — a crash leaves it done, and a re-run/resume skips it. Returns the extraction (needed for the cross-output bundle) plus a status; it inserts no cross-output/structural findings. Exceptions propagate to the caller, which distinguishes a connection blackout (abort) from a single-table error (contained)."""
    if stop.is_set():
        return {"skipped": True}        # a blackout was already detected; don't pile on
    # Re-apply the reviewer's model/effort INSIDE this thread: the server edition holds them in ContextVars, which do not propagate into pool threads.
    ai_client.configure(model=cfg.get("model"), effort=cfg.get("effort"))

    # Read this output's text AT MOST ONCE, and only if something actually needs it. pdfplumber costs ~0.6 s/page and holds the GIL, so it does not overlap with the other pool threads — it was the largest non-API cost in a run.
    pages: list[str] | None = None
    ex = _cached_extraction(o)          # unchanged file → no PDF read at all
    if ex is None:
        pages = _page_texts_for(o)
        ex = _extraction_for(o, pages)
        if ex is None:
            return {"ex": None}         # _extraction_for already recorded the error

    # Precision guard #1: re-read any extracted cell whose count/percent are internally inconsistent, before any judging fires on it. needs_self_check() is a pure detector, so it decides for free whether the source text is needed at all — and on an extraction with no suspect cells, self_check would have made no call anyway.
    if ai_review.needs_self_check(ex, config):
        if pages is None:
            pages = _page_texts_for(o)
        text = "\n".join(pages)[:60000]     # same bound range_text() applied
        ex, _n_suspect, n_corr = ai_review.self_check(text, ex, config)
        if n_corr:
            db.execute("UPDATE output SET extraction_json=? WHERE id=?",
                       (json.dumps(ex), o["id"]))

    # Prior-edition extraction → the version checklist items (6.x / 7.3 / 7.4) run inside the same within-table judge call.
    pri_ex = None
    pri_o = prior_by_label.get(_norm_label(o["label"]))
    prior_hash = None
    if pri_o:
        pri_ex = _extraction_for(pri_o)
        if pri_ex is None:
            # A matched prior means version checks are part of this table's review. Silently continuing without it would turn "comparison unavailable" into a valid zero-finding result and could auto-approve the table.
            raise RuntimeError(
                f"{o['label']}: prior-edition extraction failed; version review incomplete"
            )
        prior_hash = _content_hash_of(pri_o["id"])

    # Continuability: if this table's judge_key still matches, its within-table findings are already current — keep them and skip the (costly) judge calls. This is what a resumed run rides on; a "fresh" run cleared judge_key so it falls through and rejudges.
    jkey = _judge_key(_content_hash_of(o["id"]), prior_hash,
                      cfg.get("model"), cfg.get("effort"), cfg.get("checklist_sig"))
    if db.one("SELECT judge_key FROM output WHERE id=?", (o["id"],))["judge_key"] == jkey:
        return {"ex": ex, "cached_judge": True}

    fs = ai_review.within_table_findings(ex, o["label"], config)
    fs += ai_review.within_table_judge(ex, pri_ex, o["label"], o.get("title", ""), config)
    fs, _dropped = ai_review.verify_findings(fs, config)
    if config.get("dedupe_findings"):
        fs = checks.dedupe(fs)
    # Persist THIS table durably: replace its within-table findings, then stamp judge_key LAST so the table only counts as done once its findings are committed (a crash in between just re-judges it next time). A partial read (some pages failed) is judged on what we have but NOT stamped, so a re-run re-reads and re-judges it — and its failures are surfaced in the run errors so the banner's "check the errors" is real.
    read_errors = (ex.get("coverage") or {}).get("read_errors") or []
    if read_errors:
        with _prog_lock:
            RUN_PROGRESS.get(o["project_id"], {}).setdefault("errors", []).append(
                f"{o['label']}: {len(read_errors)} page(s) failed to read — {read_errors[0]}")
    _replace_output_within_findings(
        o["project_id"], o["id"], run_id, fs, o["label"],
        None if read_errors else jkey,
    )
    return {"ex": ex, "judged": True, "n": len(fs)}


def _coverage_row(o: dict, ex: dict) -> dict:
    """Per-output extraction coverage, for the run summary.

A table longer than ai_review._MAX_SLICES page-slices is only PARTIALLY extracted - rows past the cut never reach the model, so no judge can raise a finding on them. The run summary carries this so the UI can say "read 14 / 92 pages" instead of presenting a partial extraction as a clean review. Extractions cached before coverage existed fall back to the pages that actually produced rows."""
    cov = (ex or {}).get("coverage") or {}
    total = cov.get("pages_total") or (o["page_end"] - o["page_start"] + 1)
    read = cov.get("pages_read")
    if read is None:        # legacy cached extraction: infer from the row page stamps
        read = len({r.get("page") for r in ((ex or {}).get("summary_rows") or [])
                    if r.get("page")}) or None
    # Flag on ANY shortfall, not just the slice cap: a slice that errored also leaves pages the judges never saw. `capped` distinguishes the cause so the UI can advise raising TLF_MAX_SLICES only when that is actually the reason.
    if cov:
        short = bool(cov.get("incomplete") or cov.get("truncated"))
    else:
        short = bool(read and total and read < total)
    errs = cov.get("read_errors") or []
    groups = (ex or {}).get("groups")
    rows = (ex or {}).get("summary_rows")
    usable = bool(isinstance(groups, list) and groups and isinstance(rows, list) and rows)
    return {"label": o["label"], "pages_read": read, "pages_total": total,
            "truncated": short, "capped": bool(cov.get("truncated")),
            "usable": usable,
            # First failure reason (if the shortfall was failed reads, not the cap), so the banner can say WHY a table is partial rather than just that it is.
            "reason": (errs[0] if errs else None)}


def _coverage_summary(rows: list[dict]) -> dict:
    """Aggregate _coverage_row() output for the run summary / UI banner."""
    trunc = [c for c in rows if c.get("truncated")]
    trunc.sort(key=lambda c: (c["pages_read"] or 0) / max(1, c["pages_total"] or 1))
    unusable = [c for c in rows if not c.get("usable")]
    return {"pages_total": sum(c["pages_total"] or 0 for c in rows),
            "pages_read": sum(c["pages_read"] or 0 for c in rows),
            "n_outputs": len(rows), "n_truncated": len(trunc),
            "n_unusable": len(unusable),
            "truncated": trunc[:25],
            "unusable": [{"label": c["label"], "reason": "no groups or numeric rows"}
                         for c in unusable[:25]]}


def _demote_autoapproved(output_ids: list[int]) -> int:
    """Revoke only system-owned clean statuses; human workflow states are sacred."""
    ids = [int(i) for i in output_ids if i]
    if not ids:
        return 0
    qs = ",".join("?" * len(ids))
    before = db.one(
        f"SELECT COUNT(*) c FROM output WHERE status='Auto-approved' AND id IN ({qs})",
        tuple(ids),
    )["c"]
    db.execute(
        f"UPDATE output SET status='Not Reviewed' "
        f"WHERE status='Auto-approved' AND id IN ({qs})",
        tuple(ids),
    )
    return before


def _blocked_output_ids(pid: int, label_to_id: dict[str, int]) -> set[int]:
    """Every output implicated by a finding, including all `affected` labels.

Cross-output findings have one primary output_id but may compare several tables; relying on only the primary would allow the remaining affected tables to be auto-approved."""
    blocked: set[int] = set()
    for row in db.query("SELECT output_id, affected FROM finding WHERE project_id=?", (pid,)):
        if row.get("output_id"):
            blocked.add(int(row["output_id"]))
        for label in db.loads(row.get("affected"), []) or []:
            oid = label_to_id.get(_norm_label(str(label)))
            if oid:
                blocked.add(int(oid))
    return blocked


# A cross-output judge call carries EVERY row of every table (see ai_review._compact), which on a 70-table delivery is ~445k tokens — more than a 200k-context model takes in one call. So the bundle is split and judged in several calls. Budget is in CHARS (~4 chars/token); 400k chars ~= 100k tokens, leaving room for the checklist, schema, thinking and output.
_XOUT_CHUNK_CHARS = int(os.environ.get("TLF_XOUT_CHUNK_CHARS", "400000") or 400000)
_HUB_LABEL = "table 1"          # the exposure/grouping hub items 3/4/8 reconcile against


def _bundle_entry(o: dict, ex: dict) -> dict:
    """One output's contribution to the cross-output judge bundle: groups + footnotes + every body row. Table 1 no longer needs a special case — all tables send all rows."""
    entry = ai_review._compact(ex)
    entry["label"] = o["label"]
    entry["title"] = o.get("title", "")
    return entry


def _family(label: str) -> str:
    """The output family a label belongs to: "Table 2.2.1" -> "2.2".

Chunk boundaries must not fall INSIDE a family. Item 7.1 reconciles an AE-overview table against its SOC/PT counterpart, and those are siblings (2.2.1 / 2.2.2 / …); if they landed in different chunks, no single call would see both and the comparison would silently not happen."""
    m = re.search(r"(\d+(?:\.\d+)*)", label or "")
    if not m:
        return (label or "").lower()
    return ".".join(m.group(1).split(".")[:2])


def _chunk_bundle(bundle: list[dict]) -> list[list[dict]]:
    """Split the bundle into context-sized chunks, with the hub table in EVERY chunk.

Items 3 (pooled), 4 (by-study) and 8 (by-study vs Table 1) all reconcile against Table 1, so a chunk without it could not evaluate them. The hub is therefore repeated; the resulting duplicate findings are collapsed by checks.dedupe.

Tables are grouped into families first (see _family) so sibling tables stay in the same call. A family larger than the budget still goes out whole rather than being split — a too-large call is a visible error, whereas a silently split family is a comparison that just never happens."""
    if not bundle:
        return []
    hub = next((e for e in bundle if _norm_label(e.get("label", "")) == _HUB_LABEL), None)
    hub_len = len(json.dumps(hub, ensure_ascii=False)) if hub else 0

    fams: dict[str, list[dict]] = {}
    for e in bundle:
        if e is hub:
            continue
        fams.setdefault(_family(e.get("label", "")), []).append(e)

    chunks: list[list[dict]] = []
    cur: list[dict] = []
    cur_len = 0
    for fam in fams.values():           # dict preserves first-seen (seq) order
        n = sum(len(json.dumps(e, ensure_ascii=False)) for e in fam)
        if cur and cur_len + n + hub_len > _XOUT_CHUNK_CHARS:
            chunks.append(cur)
            cur, cur_len = [], 0
        cur.extend(fam)
        cur_len += n
    if cur:
        chunks.append(cur)
    if not chunks:                      # hub-only delivery
        return [[hub]] if hub else []
    return [([hub] + c if hub else c) for c in chunks]


def _xout_chunk(chunk: list[dict], config: dict, cfg: dict) -> list[dict]:
    """One cross-output judge call, in a pool thread (hence the re-configure)."""
    ai_client.configure(model=cfg.get("model"), effort=cfg.get("effort"))
    return ai_review.cross_output_judge(chunk, config) or []


def _select_targets(current_outputs: list[dict]) -> tuple[list[dict], int]:
    """Every TABLE in the current edition is analyzed (listings/figures are not).

Returns (targets, n_skipped); n_skipped is the outputs NOT reviewed — non-table outputs plus any tables beyond the safety ceiling (which real TLF sets never hit)."""
    def is_table(o):
        return ((o.get("output_type") or "").lower() == "table"
                or o["label"].lower().startswith("table"))
    tables = [o for o in current_outputs if is_table(o)]
    # Stable priority: Table 1 first, then by sequence, so the highest-risk reconciliation table is always covered even if the ceiling ever trims.
    tables.sort(key=lambda o: (o["label"].lower() != "table 1", o["seq"]))
    targets = tables[:MAX_CURRENT_TARGETS]
    return targets, max(0, len(current_outputs) - len(targets))


def _n_slices(o: dict) -> int:
    """Extraction calls one output costs: ~one per page, bounded by the slice cap. (A page denser than _CHUNK_CHARS splits further, so this is a floor.)"""
    pages = max(1, (o.get("page_end") or 1) - (o.get("page_start") or 1) + 1)
    return min(ai_review._MAX_SLICES, pages)


def count_targets(pid: int) -> dict:
    """What a run would do, for the pre-run time estimate.

The estimate's accuracy hinges on the cache: extraction (per PAGE, ~0.6 s + one API call each) is the bulk of a COLD run, but an output that already has `src_hash` takes the no-PDF-read fast path and costs nothing. So `extract_calls` counts only the pages that will actually be extracted — zero on a warm re-run, the full delivery on a cold one. `cached` is how many target tables are already done. Judges re-run regardless, so they are counted from `targets`, not here."""
    current, prior = _pick_current_prior(pid)
    if not current:
        return {"targets": 0, "skipped": 0, "prior": False, "extract_calls": 0, "cached": 0}
    outs = db.query(
        "SELECT * FROM output WHERE project_id=? AND document_id=? ORDER BY seq",
        (pid, current["id"]))
    targets, skipped = _select_targets(outs)
    # src_hash present ⇒ _cached_extraction will serve it without reading the PDF.
    todo = lambda o: 0 if o.get("src_hash") else _n_slices(o)
    calls = sum(todo(o) for o in targets)
    cached = sum(1 for o in targets if o.get("src_hash"))
    if prior:
        pri = {_norm_label(p["label"]): p for p in db.query(
            "SELECT * FROM output WHERE document_id=?", (prior["id"],))}
        calls += sum(todo(pri[_norm_label(o["label"])]) for o in targets
                     if _norm_label(o["label"]) in pri)
    return {"targets": len(targets), "skipped": skipped, "prior": bool(prior),
            "extract_calls": calls, "cached": cached}


def measured_throughput(pid: int):
    """Median wall-clock SECONDS PER TARGET from this project's own completed AI runs.

Modelled constants chronically under-count real time — the gateway adds per-call latency and retries, and effective concurrency is far below MAX_INFLIGHT under rate limiting. Actual history is the only reliable signal, so once a run has finished we calibrate the estimate against it. Elapsed time is normalised by the run's own target count (stored in summary_json), so incremental and fresh runs are comparable. Only clean, completed runs with a positive target count are sampled; the median over recent runs smooths cold-vs-warm variation. Returns a float, or None when there is no usable history."""
    rows = db.query(
        "SELECT started_at, finished_at, summary_json FROM ai_run "
        "WHERE project_id=? AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 10", (pid,))
    samples = []
    for r in rows:
        try:
            s = json.loads(r["summary_json"] or "{}")
        except Exception:
            continue
        n = s.get("targets", 0)
        if not n or s.get("error") or s.get("ai_unreachable"):
            continue
        try:
            elapsed = (datetime.fromisoformat(r["finished_at"])
                       - datetime.fromisoformat(r["started_at"])).total_seconds()
        except Exception:
            continue
        if elapsed > 0:
            samples.append(elapsed / n)
    if not samples:
        return None
    samples.sort()
    return samples[len(samples) // 2]


def start_run(pid: int, kind: str = "incremental",
              model: str | None = None, effort: str | None = None) -> dict:
    current, prior = _pick_current_prior(pid)
    if not current:
        return {"error": "no delivery document"}
    # Resolve everything that defines the run before spawning it.  In particular, do not read mutable client configuration later inside the background thread.
    config = ai_review.load_config()
    current_outputs = db.query(
        "SELECT * FROM output WHERE project_id=? AND document_id=? ORDER BY seq",
        (pid, current["id"]))
    targets, skipped = _select_targets(current_outputs)
    lease = _acquire_project_lease(pid)
    run_id = None
    try:
        cfg = _capture_run_config(model, effort)
        cfg["checklist_sig"] = _checklist_sig(config)
        run_id = db.insert("ai_run", project_id=pid, kind=kind, started_at=db.now_iso(),
                           summary_json=json.dumps({"status": "running",
                                                    "review_complete": False,
                                                    "model": cfg.get("model"),
                                                    "effort": cfg.get("effort")}))
        with _prog_lock:
            RUN_PROGRESS[pid] = {"running": True, "done": 0, "total": len(targets),
                                 "message": "starting", "run_id": run_id, "skipped": skipped,
                                 "errors": []}
        t = threading.Thread(
            target=_do_run,
            args=(pid, run_id, kind, current, prior, targets, config, cfg, lease),
            daemon=True,
        )
        t.start()
        return {"run_id": run_id, "total": len(targets), "skipped": skipped,
                "model": cfg.get("model"), "effort": cfg.get("effort")}
    except Exception as exc:
        if run_id is not None:
            summary = {"status": "failed", "review_complete": False,
                       "targets": len(targets), "errors": [str(exc)],
                       "auto_approved": 0, "model": (locals().get("cfg") or {}).get("model"),
                       "effort": (locals().get("cfg") or {}).get("effort")}
            db.execute("UPDATE ai_run SET finished_at=?, summary_json=? WHERE id=?",
                       (db.now_iso(), json.dumps(summary), run_id))
        _release_project_lease(pid, lease)
        raise


def run_structural_checks(pid: int, run_id=None, config=None,
                          current=None, prior=None) -> int:
    """Run the deterministic (non-AI) STRUCTURAL checks for a project and (re)write their findings. Idempotent: replaces any existing phase='structural' findings, so it is safe to call repeatedly. Needs no API key — used both at project creation (so structural issues are visible before any AI review) and inside a full run. Returns the number of structural findings written."""
    if current is None:
        current, prior = _pick_current_prior(pid)
    if not current:
        return 0
    if config is None:
        config = ai_review.load_config()
    struct = config.get("structural_checks", {})

    cur_outputs = db.query(
        "SELECT * FROM output WHERE document_id=? ORDER BY seq", (current["id"],))
    label_to_id = {_norm_label(o["label"]): o["id"] for o in cur_outputs}

    # First collect the entire replacement.  The existing structural snapshot is not touched unless every deterministic check finishes successfully.
    pending: list[tuple[int | None, dict, str | None]] = []
    # 1.3 Numbering gap over the current edition's full index.
    if struct.get("numbering_gap", True):
        cur_numbers = [r["number"] for r in cur_outputs if r["number"]]
        for f in checks.toc_gap_findings(cur_numbers):
            primary = (f.get("affected") or [None])[0]
            pending.append((None, f, primary))

    # 1.2 Missing outputs vs prior edition.
    if struct.get("missing_outputs_vs_prior") and prior:
        prior_labels = [r["label"] for r in db.query(
            "SELECT label FROM output WHERE document_id=?", (prior["id"],))]
        for f in checks.missing_output_findings([o["label"] for o in cur_outputs], prior_labels):
            primary = (f.get("affected") or [None])[0]
            pending.append((None, f, primary))

    # 1.1 Blank / empty pages across the current delivery.
    if struct.get("blank_pages"):
        for f in checks.blank_page_findings(current["path"], cur_outputs):
            lbl = f.pop("_output_label", None)
            oid = label_to_id.get(_norm_label(lbl or ""))
            pending.append((oid, f, lbl))
    _replace_phase_findings(pid, "structural", run_id, pending)
    return len(pending)


def _do_run(pid, run_id, kind, current, prior, targets,
            config=None, cfg=None, lease=None):
    prog = RUN_PROGRESS[pid]
    target_ids = [o["id"] for o in targets]
    try:
        if config is None:
            config = ai_review.load_config()
        if cfg is None:
            cfg = dict(ai_client.run_config())
            cfg["checklist_sig"] = _checklist_sig(config)
        # Preflight: one cheap connectivity check before grinding through every output. Only meaningful when the AI is configured and there's work to do. A blackout here aborts the run LOUDLY instead of "finishing" with no findings.
        if targets and ai_client.available():
            ok, detail = ai_client.preflight()
            if not ok:
                msg = (f"AI couldn't reach the API — preflight failed ({detail}). "
                       f"Run aborted; this is NOT a clean review.")
                summary = {"targets": len(targets), "findings": 0,
                           "skipped": prog.get("skipped", 0), "errors": [msg],
                           "n_failed": len(targets), "n_conn_errors": len(targets),
                           "n_judge_failed": 0, "ai_unreachable": True,
                           "auto_approved": 0, "status": "failed",
                           "review_complete": False,
                           "model": cfg.get("model"), "effort": cfg.get("effort")}
                _demote_autoapproved(target_ids)
                db.execute("UPDATE ai_run SET finished_at=?, summary_json=? WHERE id=?",
                           (db.now_iso(), json.dumps(summary), run_id))
                prog.update(running=False, message=msg, summary=summary)
                return
        # "fresh" = re-read THIS edition from scratch (the reviewer distrusts the current reading). It deliberately does NOT touch the prior edition: last year's PDF is immutable, so re-extracting it costs hundreds of API calls to reproduce byte- identical output. content_hash still guards correctness — if a prior PDF really is replaced, its hash stops matching and it re-extracts on its own. "rebuild" = clear EVERY edition, for when the extraction prompt/schema changed and cached extractions are in the old format. judge_key is cleared alongside the extraction cache so those tables re-judge.
        if kind == "fresh":
            _demote_autoapproved(target_ids)
            db.execute("UPDATE output SET extraction_json=NULL, content_hash=NULL, "
                       "src_hash=NULL, judge_key=NULL WHERE document_id=?", (current["id"],))
        elif kind == "rebuild":
            _demote_autoapproved(target_ids)
            db.execute("UPDATE output SET extraction_json=NULL, content_hash=NULL, "
                       "src_hash=NULL, judge_key=NULL WHERE project_id=?", (pid,))

        # Do not clear cross/structural findings here.  They are the last complete published snapshot and are atomically replaced only after the new phase finishes successfully. Imported findings are always preserved.

        prior_by_label = {}
        if prior:
            for o in db.query("SELECT * FROM output WHERE document_id=?", (prior["id"],)):
                prior_by_label[_norm_label(o["label"])] = o

        extractions: list[tuple[dict, dict]] = []   # (output, extraction) in target order
        cov_rows: list[dict] = []                   # per-output extraction coverage
        n_findings = 0
        n_extract_failed = 0    # extraction returned None — drives the ai_unreachable backstop
        n_judge_failed = 0      # a non-connection error while judging one table (contained)
        # Tables are analyzed CONCURRENTLY. Each table's work is independent (its own extraction + judge), and every step is an API wait, so the run is I/O-bound rather than CPU-bound. ai_client._create caps total in-flight requests, so the pool size here is just how many tables may be in flight — the gateway is protected by that one global semaphore, not by this number.
        stop = threading.Event()          # set on a connection blackout → fail fast
        results: list[dict | None] = [None] * len(targets)
        with cf.ThreadPoolExecutor(max_workers=max(1, ai_client.MAX_INFLIGHT)) as pool:
            fut = {pool.submit(_analyze_table, o, config, prior_by_label, cfg, run_id, stop): i
                   for i, o in enumerate(targets)}
            for f in cf.as_completed(fut):
                i = fut[f]
                o = targets[i]
                try:
                    results[i] = f.result()
                except Exception as e:
                    # A genuine connectivity blackout must still abort the whole run loudly. We cannot un-submit work already queued, so signal `stop` and let the queued tables return immediately; the error is re-raised once the pool drains.
                    if ai_client.is_connection_error(e):
                        stop.set()
                        results[i] = {"conn_error": e}
                    else:
                        # Any OTHER per-table error is contained so ONE table can't collapse the run.
                        results[i] = {"error": f"{o['label']}: {e}"}
                with _prog_lock:
                    prog["done"] += 1
                    # Naming the table that just finished reads as random jumping once tables run concurrently (completion order is arbitrary). A count is monotonic and actually tells the reviewer where the run is.
                    prog["message"] = f"analyzed {prog['done']}/{len(targets)} tables"

        # Fold the results in TARGET ORDER. Within-table findings were already persisted by each worker (durably, per table); here we only collect extractions for the cross-output bundle and tally what happened.
        n_judged = n_cached = 0
        for i, o in enumerate(targets):
            r = results[i] or {"error": f"{o['label']}: no result"}
            if r.get("conn_error"):
                raise r["conn_error"]         # → outer handler aborts the run loudly
            if r.get("error"):
                prog.setdefault("errors", []).append(r["error"])
                n_judge_failed += 1
                continue
            if r.get("skipped") or r.get("ex") is None:
                n_extract_failed += 1      # _extraction_for already logged any error
                continue
            extractions.append((o, r["ex"]))
            cov_rows.append(_coverage_row(o, r["ex"]))
            n_cached += 1 if r.get("cached_judge") else 0
            n_judged += 1 if r.get("judged") else 0

        label_to_id = {_norm_label(r["label"]): r["id"] for r in db.query(
            "SELECT id, label FROM output WHERE document_id=?", (current["id"],))}

        coverage = _coverage_summary(cov_rows)
        n_completed_tables = n_judged + n_cached
        table_phase_complete = (
            bool(targets)
            and len(extractions) == len(targets)
            and n_completed_tables == len(targets)
            and n_extract_failed == 0
            and n_judge_failed == 0
            and coverage.get("n_truncated", 0) == 0
            and coverage.get("n_unusable", 0) == 0
        )

        # --- Cross-output AI judge (checklist 3 / 4 / 5 / 7.1 / 8) ---------- #
        cross_ok = False
        if table_phase_complete:
            bundle = [_bundle_entry(o, ex) for (o, ex) in extractions]
            chunks = _chunk_bundle(bundle)
            prog["message"] = ("cross-output review" if len(chunks) <= 1
                               else f"cross-output review ({len(chunks)} parts)")
            xf_all: list[dict] = []
            conn_fatal: Exception | None = None
            cross_ok = True
            # Chunks are independent calls, so run them concurrently under the same global in-flight cap as everything else.
            with cf.ThreadPoolExecutor(max_workers=max(1, ai_client.MAX_INFLIGHT)) as pool:
                futs = [pool.submit(_xout_chunk, ch, config, cfg) for ch in chunks]
                for f in cf.as_completed(futs):
                    try:
                        xf_all.extend(f.result())
                    except Exception as e:
                        if ai_client.is_connection_error(e):
                            conn_fatal = e
                        else:
                            prog.setdefault("errors", []).append(f"cross-output: {e}")
                            n_judge_failed += 1
                            cross_ok = False
            if conn_fatal is not None:
                raise conn_fatal
            if cross_ok:
                # The hub table rides in every chunk, so two chunks can report the same discrepancy — dedupe collapses those before anything is stored.
                xf, _dropped = ai_review.verify_findings(xf_all, config)
                if config.get("dedupe_findings"):
                    xf = checks.dedupe(xf)
                pending_cross: list[tuple[int | None, dict, str | None]] = []
                for f in xf:
                    aff = f.get("affected") or []
                    primary = aff[0] if aff else None
                    oid = label_to_id.get(_norm_label(primary or ""))
                    pending_cross.append((oid, f, primary))
                _replace_phase_findings(pid, "cross", run_id, pending_cross)

        # --- Deterministic STRUCTURAL checks ------------------------------- # Same checks that run at project creation; re-run here (idempotent — replaces phase='structural') so a fresh/rebuild run refreshes them against this index.
        prog["message"] = "structural checks"
        structural_ok = True
        try:
            run_structural_checks(pid, run_id=run_id, config=config,
                                  current=current, prior=prior)
        except Exception as e:
            structural_ok = False
            n_judge_failed += 1
            prog.setdefault("errors", []).append(f"structural checks: {e}")

        # Total findings now living for this project (within-table were written per worker; cross + structural just above). Counting from the DB is robust to the cache skips.
        n_findings = db.one("SELECT COUNT(*) c FROM finding WHERE project_id=?", (pid,))["c"]
        n_conn = prog.get("conn_errors", 0)
        # AI-unreachable = we had work to do, MOST target extractions failed, and MOST of those failures were connection errors. That's the silent all-clear the UI must flag as NOT a clean review — not a healthy run that happened to find nothing.
        ai_unreachable = (bool(targets)
                          and n_extract_failed >= (len(targets) + 1) // 2
                          and n_conn >= (n_extract_failed + 1) // 2)

        review_complete = (
            table_phase_complete and cross_ok and structural_ok
            and n_judge_failed == 0 and not prog.get("errors")
        )

        # A zero-finding result is evidence of cleanliness only after every required stage succeeds with full coverage. Any partial/failed attempt revokes stale system-owned approvals; human statuses are never overwritten.
        n_auto = 0
        if review_complete:
            blocked = _blocked_output_ids(pid, label_to_id)
            stale = [oid for oid in target_ids if oid in blocked]
            _demote_autoapproved(stale)
            clean_ids = [oid for oid in target_ids if oid not in blocked]
            if clean_ids:
                qs = ",".join("?" * len(clean_ids))
                clean = db.query(
                    f"SELECT id FROM output WHERE status='Not Reviewed' AND id IN ({qs}) "
                    , tuple(clean_ids))
                for row in clean:
                    db.execute("UPDATE output SET status='Auto-approved' WHERE id=?", (row["id"],))
                n_auto = len(clean)
                if n_auto:
                    db.audit("system", "status.auto_approve", "project", pid, pid,
                             f"{n_auto} clean tables auto-approved")
            # Legacy phase-less AI findings are safe to remove only after the new complete phased snapshot has been published.
            db.execute("DELETE FROM finding WHERE project_id=? AND phase IS NULL", (pid,))
        else:
            _demote_autoapproved(target_ids)

        status = "succeeded" if review_complete else (
            "failed" if n_completed_tables == 0 else "partial"
        )
        summary = {"targets": len(targets), "findings": n_findings,
                   "skipped": prog.get("skipped", 0), "errors": prog.get("errors", []),
                   "n_failed": n_extract_failed, "n_judge_failed": n_judge_failed,
                   "n_conn_errors": n_conn, "ai_unreachable": ai_unreachable,
                   "auto_approved": n_auto,
                   "coverage": coverage, "status": status,
                   "review_complete": review_complete,
                   "model": cfg.get("model"), "effort": cfg.get("effort")}
        db.execute("UPDATE ai_run SET finished_at=?, summary_json=? WHERE id=?",
                   (db.now_iso(), json.dumps(summary), run_id))
        done_msg = "done" if review_complete else "incomplete — review errors before relying on results"
        if ai_unreachable:
            done_msg = (f"AI couldn't reach the API — {n_extract_failed}/{len(targets)} "
                        f"extractions failed (connection error). NOT a clean review.")
        prog.update(running=False, message=done_msg, summary=summary)
    except Exception as e:
        _demote_autoapproved(target_ids)
        errors = list(prog.get("errors", []))
        if str(e) not in errors:
            errors.append(str(e))
        summary = {"status": "failed", "review_complete": False,
                   "targets": len(targets), "findings": 0,
                   "skipped": prog.get("skipped", 0), "errors": errors,
                   "n_failed": len(targets), "n_judge_failed": 0,
                   "n_conn_errors": prog.get("conn_errors", 0),
                   "ai_unreachable": ai_client.is_connection_error(e),
                   "auto_approved": 0,
                   "model": (cfg or {}).get("model"),
                   "effort": (cfg or {}).get("effort")}
        prog.update(running=False, message=f"error: {e}", summary=summary)
        db.execute("UPDATE ai_run SET finished_at=?, summary_json=? WHERE id=?",
                   (db.now_iso(), json.dumps(summary), run_id))
    finally:
        _release_project_lease(pid, lease)


def run_single_output(oid: int, model: str | None = None,
                      effort: str | None = None) -> dict:
    """Re-analyze one output (used by the per-output Re-run button). Replaces that output's findings; leaves cross-output findings untouched."""
    o = db.one("SELECT * FROM output WHERE id=?", (oid,))
    if not o:
        return {"error": "output not found"}
    config = ai_review.load_config()
    pid = o["project_id"]
    lease = _acquire_project_lease(pid)
    run_id = None
    cfg: dict = {}
    try:
        cfg = _capture_run_config(model, effort)
        cfg["checklist_sig"] = _checklist_sig(config)
        # A single-output pass does not rerun cross-output checks, so it must never leave a stale system-wide clean approval attached to the table.
        _demote_autoapproved([oid])
        run_id = db.insert(
            "ai_run", project_id=pid, kind="single", started_at=db.now_iso(),
            summary_json=json.dumps({"status": "running", "review_complete": False,
                                     "scope": "single-output", "output": o["label"],
                                     "model": cfg.get("model"), "effort": cfg.get("effort")}),
        )
        ai_client.configure(model=cfg.get("model"), effort=cfg.get("effort"))
        # One text read, reused for extraction and (only if needed) self-check.
        pages = _page_texts_for(o)
        db.execute("UPDATE output SET extraction_json=NULL, content_hash=NULL, "
                   "src_hash=NULL, judge_key=NULL WHERE id=?", (oid,))
        ex = _extraction_for(o, pages)
        if ex is None:
            raise RuntimeError("extraction failed")
        if ai_review.needs_self_check(ex, config):
            text = "\n".join(pages)[:60000]
            ex, _n_suspect, n_corr = ai_review.self_check(text, ex, config)
            if n_corr:
                db.execute("UPDATE output SET extraction_json=? WHERE id=?",
                           (json.dumps(ex), oid))

        # Prior-edition same-labelled output → required version checks.
        current, prior = _pick_current_prior(pid)
        pri_ex = None
        prior_hash = None
        if prior and o["document_id"] == (current or {}).get("id"):
            pri_o = db.one("SELECT * FROM output WHERE document_id=? AND label=?",
                           (prior["id"], o["label"]))
            if pri_o:
                pri_ex = _extraction_for(pri_o)
                if pri_ex is None:
                    raise RuntimeError("prior-edition extraction failed; version review incomplete")
                prior_hash = _content_hash_of(pri_o["id"])

        fs = ai_review.within_table_findings(ex, o["label"], config)
        fs += ai_review.within_table_judge(ex, pri_ex, o["label"], o.get("title", ""), config)
        fs, _dropped = ai_review.verify_findings(fs, config)
        if config.get("dedupe_findings"):
            fs = checks.dedupe(fs)
        cov = _coverage_row(o, ex)
        complete = not cov.get("truncated")
        jkey = _judge_key(_content_hash_of(oid), prior_hash, cfg.get("model"),
                          cfg.get("effort"), cfg.get("checklist_sig"))
        _replace_output_within_findings(pid, oid, run_id, fs, o["label"],
                                        jkey if complete else None)
        summary = {"status": "succeeded" if complete else "partial",
                   "review_complete": complete, "scope": "single-output",
                   "output": o["label"], "findings": len(fs),
                   "coverage": _coverage_summary([cov]), "errors": [],
                   "auto_approved": 0, "model": cfg.get("model"),
                   "effort": cfg.get("effort")}
        db.execute("UPDATE ai_run SET finished_at=?, summary_json=? WHERE id=?",
                   (db.now_iso(), json.dumps(summary), run_id))
        return {"ok": complete, "findings": len(fs), "run_id": run_id,
                "review_complete": complete,
                **({"error": "output was only partially read"} if not complete else {})}
    except Exception as e:
        summary = {"status": "failed", "review_complete": False,
                   "scope": "single-output", "output": o["label"],
                   "findings": 0, "errors": [str(e)], "auto_approved": 0,
                   "model": cfg.get("model"), "effort": cfg.get("effort")}
        if run_id is not None:
            db.execute("UPDATE ai_run SET finished_at=?, summary_json=? WHERE id=?",
                       (db.now_iso(), json.dumps(summary), run_id))
        return {"ok": False, "error": str(e), "run_id": run_id,
                "review_complete": False}
    finally:
        _release_project_lease(pid, lease)


def _insert_finding(pid, output_id, run_id, f: dict, output_label: str | None = None,
                    phase: str = "within", conn=None):
    sig = checks.finding_signature(f.get("check_id", ""), output_label,
                                   f.get("numbers", []), f.get("message", ""))
    cols = {
        "project_id": pid, "output_id": output_id, "run_id": run_id,
        "check_id": f["check_id"], "severity": f.get("severity", "low"),
        "risk": f.get("risk", ""), "message": f["message"],
        "subjects": json.dumps(f.get("subjects", [])),
        "numbers": json.dumps(f.get("numbers", [])), "page": f.get("page"),
        "printed_page": f.get("printed_page"), "pages_total": f.get("pages_total"),
        "section": f.get("section"), "row_kind": f.get("row_kind"),
        "signature": sig, "state": "pending", "badge": "", "phase": phase,
        "affected": json.dumps(f.get("affected", [])),
    }
    if conn is None:
        db.insert("finding", **cols)
        return
    keys = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    conn.execute(f"INSERT INTO finding ({keys}) VALUES ({marks})", tuple(cols.values()))


def _replace_phase_findings(pid: int, phase: str, run_id: int | None,
                            rows: list[tuple[int | None, dict, str | None]]) -> None:
    """Atomically publish a complete project-level finding phase.

The previous phase remains visible if collection/judging fails.  Only after every replacement row exists in memory do we delete the old snapshot and insert the new one in a single SQLite transaction."""
    conn = db.get()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM finding WHERE project_id=? AND phase=?", (pid, phase))
        for output_id, finding, output_label in rows:
            _insert_finding(pid, output_id, run_id, finding, output_label,
                            phase=phase, conn=conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _replace_output_within_findings(pid: int, output_id: int, run_id: int,
                                    findings: list[dict], output_label: str,
                                    judge_key: str | None) -> None:
    """Atomically replace one table's within findings and completion stamp."""
    conn = db.get()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM finding WHERE output_id=? AND phase='within'",
                     (output_id,))
        for finding in findings:
            _insert_finding(pid, output_id, run_id, finding, output_label,
                            phase="within", conn=conn)
        conn.execute("UPDATE output SET judge_key=? WHERE id=?", (judge_key, output_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
