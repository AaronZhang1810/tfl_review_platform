"""Per-output AI extraction, the extraction self-check, and the checklist-driven judges.

The app's model calls, all forced structured output:
  * extract() — small, reliable: analysis set, treatment groups + big-N, whether it's an
    AE-by-PT table, footnote markers, a bounded set of key summary rows (with per-row
    page/section/kind for provenance), PT terms (AE tables only), and missing-N rows.
  * self_check() — precision guard: re-reads only the cells whose count/percent are
    internally inconsistent (or n>N), so judging never fires on a mis-transcribed number.
  * within_table_judge() / cross_output_judge() — the AI does the JUDGING, guided by the
    8-point checklist in study_config.json (examples are few-shot guidance, never parsed).
  * verify_findings() — Python re-checks the arithmetic each numeric finding itself cites
    and drops any whose own numbers do not actually contradict the observed value.

Only structural checks (blank pages / missing-vs-prior / numbering gap) stay deterministic;
those live in checks.py.
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import logging
import os
import re
import time

import ai_client

logger = logging.getLogger("tlf.ai")

# A single page-slice occasionally fails on a transient gateway hiccup (429 / 5xx /
# overloaded) even after the SDK's own retries. Retrying that one slice a few times
# recovers the page instead of silently dropping it. Connection errors are NOT retried
# here — they bubble up and abort the whole run loudly, as before.
_SLICE_RETRIES = max(0, int(os.environ.get("TLF_SLICE_RETRIES", "2") or 2))


def _extract_slice_resilient(text: str, label: str, known_groups: list, page: int):
    """_extract_slice with a bounded retry on transient (non-connection) errors.
    Returns the slice dict, or raises: a connection error (to abort) or the last
    transient error (so the caller can record which page failed)."""
    last = None
    for attempt in range(_SLICE_RETRIES + 1):
        try:
            return _extract_slice(text, label, known_groups, page)
        except Exception as e:
            if ai_client.is_connection_error(e):
                raise                       # blackout → abort the run, don't retry here
            last = e
            if attempt < _SLICE_RETRIES:
                time.sleep(0.6 * (attempt + 1))   # brief backoff, then retry this slice
    raise last

_ROOT = os.path.dirname(__file__)
_WORKING_CONFIG = os.path.join(_ROOT, "study_config.json")
_SYNTHETIC_CONFIG = os.path.join(_ROOT, "configs", "study_config.synthetic.json")
_CFG_PATH = os.environ.get("TLF_STUDY_CONFIG") or (
    _WORKING_CONFIG if os.path.isfile(_WORKING_CONFIG) else _SYNTHETIC_CONFIG
)


class AIReviewResponseError(RuntimeError):
    """An AI stage returned malformed output that cannot count as a clean review."""


def load_config() -> dict:
    try:
        with open(_CFG_PATH, encoding="utf-8") as fh:
            config = json.load(fh)
    except Exception as exc:
        raise RuntimeError(f"review configuration could not be loaded from {_CFG_PATH}: {exc}") from exc
    if not isinstance(config, dict) or not isinstance(config.get("checklist"), list):
        raise RuntimeError(f"review configuration is malformed: {_CFG_PATH}")
    return config


# --------------------------------------------------------------------------- #
# Main extraction
# --------------------------------------------------------------------------- #

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "analysis_set": {"type": "string", "description": "e.g. 'Safety Analysis Set'"},
        "run_datetime": {"type": "string"},
        "header_label": {"type": "string", "description": "printed table label, e.g. 'Table 1'"},
        "is_ae_pt_table": {"type": "boolean",
            "description": "true if this table lists adverse events by System Organ Class / Preferred Term"},
        "groups": {
            "type": "array",
            "description": "treatment/column groups with big-N; use these exact labels in summary_rows.values",
            "items": {"type": "object", "properties": {
                "label": {"type": "string"}, "n": {"type": ["integer", "null"]},
            }, "required": ["label"]},
        },
        "footnote_markers": {"type": "array", "items": {"type": "string"},
            "description": "footnote text lines, especially any that list the studies included per indication"},
        "summary_rows": {
            "type": "array",
            "description": "EVERY row in the table body that has at least one per-group numeric value, "
                           "for ANY table type — exposure, disposition, demographics, disease "
                           "characteristics, adverse events, etc. — NOT only adverse-event or "
                           "subject-incidence tables. Capture each category/indication row, each "
                           "per-study row (e.g. 'SYN-A101 Cohort A'), each subtotal row, and any "
                           "Total/Overall row. A '---' or blank cell just means that group's value is "
                           "null — still capture the row for the groups that do have a value; never "
                           "skip a row because some cells are blank. For a very large "
                           "adverse-event-by-Preferred-Term table, include all overview/summary rows "
                           "plus as many SOC/PT rows as fit (about 60); for every other table capture "
                           "every body row (up to about 150). Copy exactly.",
            "items": {"type": "object", "properties": {
                "label": {"type": "string"},
                "values": {"type": "object", "description": "group label -> the COUNT (n) as a number, null if blank"},
                "pcts": {"type": "object", "description": "group label -> the PERCENT as a number (e.g. 97.6). "
                                                          "REQUIRED whenever the cell is formatted 'n (%)' or "
                                                          "'n/N (%)': set both values[group]=n and pcts[group]=the "
                                                          "percent. Use null only when the cell shows no percent."},
                "page": {"type": ["integer", "null"], "description": "1-based page within THIS output where the row is printed"},
                "section": {"type": ["string", "null"], "description": "nearest preceding indication/category header for this row, e.g. 'Condition Alpha'"},
                "row_kind": {"type": ["string", "null"], "description": "'study' for a per-study/subject row (e.g. 'SYN-A102'); 'aggregate' for a subtotal/category/total row"},
            }, "required": ["label", "values"]},
        },
        "pt_terms": {"type": "array", "items": {"type": "string"},
            "description": "adverse-event Preferred Terms only (empty for non-AE tables)"},
        "missing_n_rows": {"type": "array", "items": {"type": "string"},
            "description": "row labels missing an N where one is expected"},
        "notes": {"type": "string"},
    },
    "required": ["analysis_set", "header_label", "groups"],
}

_SYS = (
    "You are a statistical reviewer extracting facts from a single clinical-trial TLF "
    "output for QC. Copy numbers exactly — never compute or round. Use null/empty when "
    "unknown. Extract the table's numeric body IN FULL: every row that shows a count for "
    "one or more groups is a summary row — including exposure, disposition, demographic, "
    "disease-characteristic, per-study and per-indication rows (e.g. 'SYN-A101', 'Condition Alpha'), "
    "category and subtotal rows, and any Total/Overall row — regardless of whether this is "
    "an adverse-event table. Do not stop after the first page; capture rows across every "
    "page of the output. In summary_rows.values, key every value by the exact group label from `groups`. "
    "Whenever a cell is formatted 'n (%)' or 'n/N (%)', you MUST populate BOTH "
    "summary_rows.values[group]=n AND summary_rows.pcts[group]=the percent number. Capture "
    "the table's Total/Overall row whenever one is printed. For each summary row also record "
    "its 1-based page within this output, the nearest preceding indication/category header "
    "(section), and whether it is a 'study' row (a per-study/subject row) or an 'aggregate' "
    "row (a subtotal/category/total). If the table lists the studies included per indication "
    "in a footnote, capture those footnote lines."
)


# Above ~5-6 KB of table text in one structured-output call the model returns ZERO
# summary_rows for a wide multi-page table — even though it describes the table correctly
# in `notes`. It is a recall cliff, not a schema or token-budget problem: the SAME schema
# extracts every row cleanly from a single page (~2 KB). So a big table is extracted one
# page-slice at a time (each kept under the cliff) and merged. Every row is stamped with
# the exact page it came from, which also gives findings a real page instead of page 1.
_CHUNK_CHARS = 4500        # per-slice text ceiling, kept safely under the recall cliff

# Extraction is mechanical TRANSCRIPTION, not judgement: it re-types numbers already
# printed on the page, and the two precision guards (self_check, verify_findings) exist
# precisely to catch it getting one wrong. It is also ~85% of a run's API calls. So it
# runs on the small fast model while the JUDGES keep the reviewer's chosen model, where
# reasoning actually decides whether something is a finding.
# Set TLF_FAST_EXTRACT=0 to extract with the run's model instead.
_FAST_EXTRACT = (os.environ.get("TLF_FAST_EXTRACT", "1").strip().lower()
                 not in ("0", "false", "no", "off"))


def _extract_model() -> str | None:
    """The model to extract with: the fast one when enabled AND the key actually grants
    it, otherwise None = fall through to the run's model. Never let a gateway that
    doesn't serve the fast model break extraction."""
    if not _FAST_EXTRACT:
        return None
    try:
        if ai_client.FAST_MODEL in {m["id"] for m in ai_client.available_models()}:
            return ai_client.FAST_MODEL
    except Exception:
        pass
    return None
# Cap on extraction calls per output. This is a COVERAGE control, not just a cost knob:
# slices past the cap are never sent to the model, so no judge can raise a finding on
# those pages and "0 findings" on a long table would not mean it is clean. It used to be
# 14, which read only the first 14 pages of a 92-page table (~15%). Now that extraction
# runs concurrently on the fast model, full coverage is affordable, so the default is
# high enough not to truncate real deliveries.
#   TLF_MAX_SLICES=0  -> no limit at all
#   TLF_MAX_SLICES=14 -> the old, deliberately cheap-and-partial behaviour
_MAX_SLICES = int(os.environ.get("TLF_MAX_SLICES", "400") or 400)
if _MAX_SLICES <= 0:
    _MAX_SLICES = 10 ** 9      # 0 = unlimited


def _coverage(pages_total: int, pages_read: int, slices_total: int, slices_used: int,
              slices_ok: int | None = None) -> dict:
    """How much of the output actually reached the model.

    Recorded on every extraction (and persisted inside extraction_json) so the UI can
    say "read 14 / 92 pages" instead of silently presenting a partial extraction as a
    complete review.

    Two distinct shortfalls, kept separate because they need different fixes:
      * ``truncated``  — slices past ``_MAX_SLICES`` were never attempted. Fix: raise the cap.
      * ``incomplete`` — fewer pages came back than the output has, for ANY reason
        (the cap, or a slice that errored). This is the one the reviewer must see.
    ``pages_read`` counts only pages whose slice actually merged.
    """
    ok = slices_used if slices_ok is None else slices_ok
    return {"pages_total": pages_total, "pages_read": pages_read,
            "slices_total": slices_total, "slices_used": slices_used, "slices_ok": ok,
            "truncated": slices_used < slices_total,
            "incomplete": pages_read < pages_total}


def extract(output_text: str, label: str, page_texts: list[str] | None = None) -> dict:
    """Extract one output's structured fields. When ``page_texts`` (per-page text, in
    output order) is supplied and the output is large, extract page-by-page and merge so
    the model never faces more than one page at a time; otherwise a single call.

    The returned dict always carries a ``coverage`` key (see :func:`_coverage`)."""
    pages = [p or "" for p in page_texts] if page_texts else None
    whole = output_text if output_text is not None else ("\n".join(pages) if pages else "")
    if not pages or len(pages) <= 1 or len(whole) <= _CHUNK_CHARS:
        ex = _extract_once(whole, label)            # small output: one call (as before)
        n = len(pages) if pages else 1
        ex["coverage"] = _coverage(n, n, 1, 1)      # whole output seen in one call
        return ex
    return _extract_paged(pages, label)


def _extract_once(text: str, label: str) -> dict:
    user = (f"Output label: {label}\n\n--- OUTPUT TEXT (may be truncated) ---\n"
            f"{text}\n--- END ---\n\nExtract the structured fields.")
    data = ai_client.call_structured(_SYS, user, "extraction", EXTRACTION_SCHEMA,
                                     model=_extract_model(), max_tokens=8000, effort="low")
    return _validate_extraction_response(data)


def _extract_slice(text: str, label: str, known_groups: list[str], page: int) -> dict:
    hint = ""
    if known_groups:
        hint = ("\nThe table's treatment/column groups are exactly: "
                f"{json.dumps(known_groups, ensure_ascii=False)}. Key every "
                "summary_rows.values entry by one of these exact labels.")
    user = (f"Output label: {label}\n\nThis is page {page} of the output. Extract EVERY "
            "body row shown on THIS page into summary_rows (there may be many); copy "
            "numbers exactly." + hint +
            f"\n\n--- OUTPUT PAGE TEXT ---\n{text}\n--- END ---\n\nExtract the structured fields.")
    data = ai_client.call_structured(_SYS, user, "extraction", EXTRACTION_SCHEMA,
                                     model=_extract_model(), max_tokens=8000, effort="low")
    return _validate_extraction_response(data)


def _validate_extraction_response(data) -> dict:
    """Reject malformed structured output before it can count toward coverage.

    Provider-side schema enforcement is helpful but is not a trust boundary: a proxy,
    SDK change, test double, or truncated response can still return the wrong shape.
    Valid-but-empty lists are allowed here because a continuation/footnote page may have
    no body rows; the runner separately requires usable evidence for a clean decision.
    """
    if not isinstance(data, dict):
        raise AIReviewResponseError("extraction response is not an object")
    for key in ("analysis_set", "header_label"):
        if not isinstance(data.get(key), str):
            raise AIReviewResponseError(f"extraction field '{key}' is not a string")
    for key in ("groups", "summary_rows", "footnote_markers", "pt_terms", "missing_n_rows"):
        if key in data and not isinstance(data[key], list):
            raise AIReviewResponseError(f"extraction field '{key}' is not a list")
        data.setdefault(key, [])
    for group in data["groups"]:
        if (not isinstance(group, dict) or not isinstance(group.get("label"), str)
                or not group.get("label").strip()):
            raise AIReviewResponseError("extraction contains a malformed group")
        n = group.get("n")
        if n is not None and (not isinstance(n, int) or isinstance(n, bool)):
            raise AIReviewResponseError("extraction group denominator is not an integer or null")
    for row in data["summary_rows"]:
        if (not isinstance(row, dict) or not isinstance(row.get("label"), str)
                or not row.get("label").strip() or not isinstance(row.get("values"), dict)):
            raise AIReviewResponseError("extraction contains a malformed summary row")
        if "pcts" in row and row["pcts"] is not None and not isinstance(row["pcts"], dict):
            raise AIReviewResponseError("extraction row percentages are not an object")
    for key in ("footnote_markers", "pt_terms", "missing_n_rows"):
        if any(not isinstance(value, str) for value in data[key]):
            raise AIReviewResponseError(f"extraction field '{key}' contains a non-string value")
    if "is_ae_pt_table" in data and not isinstance(data["is_ae_pt_table"], bool):
        raise AIReviewResponseError("extraction field 'is_ae_pt_table' is not boolean")
    return data


def _slices(pages: list[str]) -> list[dict]:
    """One {page, text} slice per page (page = 1-based within the output), so every row
    gets an exact page. An oversized page is split on line boundaries, keeping its page."""
    out: list[dict] = []
    for pno, txt in enumerate(pages, start=1):
        t = txt or ""
        if len(t) <= _CHUNK_CHARS:
            out.append({"page": pno, "text": t})
            continue
        buf: list[str] = []
        blen = 0
        for line in t.splitlines():
            if buf and blen + len(line) > _CHUNK_CHARS:
                out.append({"page": pno, "text": "\n".join(buf)})
                buf, blen = [], 0
            buf.append(line)
            blen += len(line) + 1
        if buf:
            out.append({"page": pno, "text": "\n".join(buf)})
    return out


def _extract_paged(pages: list[str], label: str) -> dict:
    all_slices = _slices(pages)
    slices = all_slices[:_MAX_SLICES]
    truncated = len(all_slices) > len(slices)
    merged = {"analysis_set": "", "run_datetime": "", "header_label": label,
              "is_ae_pt_table": False, "groups": [], "footnote_markers": [],
              "summary_rows": [], "pt_terms": [], "missing_n_rows": [], "notes": ""}
    known_groups: list[str] = []
    got_any = False
    # Pages whose slice actually came back and was merged. Coverage MUST be derived from
    # this, not from the slices we attempted: a slice that errored contributes no rows, so
    # counting it as "read" would report a partial extraction as complete — the exact
    # failure this coverage data exists to expose.
    read_pages: set[int] = set()
    read_errors: list[str] = []          # (page, short message) for slices that finally failed

    def _note_fail(page: int, e: Exception) -> None:
        msg = (str(e) or type(e).__name__).splitlines()[0][:160]
        read_errors.append(f"p{page}: {msg}")
        logger.warning("extract '%s' page %s failed after retries: %s", label, page, msg)

    # Slice 1 runs ALONE first: it is what teaches us the table's column-group labels,
    # which every later slice is told to key its values by. Only then can the rest go
    # out concurrently. Without this the group labels would be learned by whichever
    # slice happened to land first, and different slices could key the same column
    # differently — so the merged extraction would not line up.
    first, rest = (slices[0], slices[1:]) if slices else (None, [])
    if first is not None:
        try:
            part = _extract_slice_resilient(first["text"], label, known_groups, first["page"])
            got_any = True
            _merge_into(merged, part, first["page"])
            read_pages.add(first["page"])
            known_groups = [g["label"] for g in merged["groups"] if g.get("label")]
        except Exception as e:
            if ai_client.is_connection_error(e):
                raise                    # abort the run loudly, same as a single-call failure
            _note_fail(first["page"], e)

    # The remaining slices are independent given known_groups. They are pure I/O waits,
    # so run them concurrently — ai_client._create caps total in-flight calls globally,
    # which is what keeps this from stampeding the gateway. Results are merged in SLICE
    # ORDER, not completion order, so the extraction stays deterministic.
    parts: list[tuple[dict, int] | None] = [None] * len(rest)
    if rest:
        conn_err: list[Exception] = []
        with cf.ThreadPoolExecutor(max_workers=max(1, ai_client.MAX_INFLIGHT)) as pool:
            fut = {pool.submit(_extract_slice_resilient, sl["text"], label, known_groups, sl["page"]): i
                   for i, sl in enumerate(rest)}
            for f in cf.as_completed(fut):
                i = fut[f]
                try:
                    parts[i] = (f.result(), rest[i]["page"])
                except Exception as e:
                    if ai_client.is_connection_error(e):
                        conn_err.append(e)
                    else:
                        _note_fail(rest[i]["page"], e)   # a slice lost after its retries
        if conn_err and not got_any:
            raise conn_err[0]            # nothing usable AND the API is down → fail loudly
    n_ok = 1 if read_pages else 0        # the first slice, if it succeeded
    for p in parts:
        if p is None:
            continue
        got_any = True
        n_ok += 1
        _merge_into(merged, p[0], p[1])
        read_pages.add(p[1])
    if not got_any:
        ex = _extract_once("\n".join(pages), label)     # every slice failed → one whole call
        ex["coverage"] = _coverage(len(pages), len(pages), 1, 1)
        return ex
    merged["coverage"] = _coverage(len(pages), len(read_pages),
                                   len(all_slices), len(slices), slices_ok=n_ok)
    if read_errors:
        # Surfaced by the runner into the run's errors + used to withhold the fast-path
        # cache so a plain re-run RETRIES these pages instead of reusing the gap.
        merged["coverage"]["read_errors"] = read_errors[:10]
    if truncated:
        tail = (f"[extraction limited to the first {len(slices)} page-slices of "
                f"{len(all_slices)} ({len(pages)} pages)]")
        merged["notes"] = (merged["notes"] + " " + tail).strip()
    return merged


def _merge_into(merged: dict, part: dict, page: int) -> None:
    part = _validate_extraction_response(part)
    for k in ("analysis_set", "run_datetime", "header_label"):
        if not merged.get(k) and part.get(k):
            merged[k] = part[k]
    if part.get("is_ae_pt_table"):
        merged["is_ae_pt_table"] = True
    _merge_groups(merged["groups"], part.get("groups"))
    for r in part.get("summary_rows") or []:
        if isinstance(r, dict):
            r["page"] = page             # exact page within the output (overrides model guess)
            merged["summary_rows"].append(r)
    _extend_unique(merged["footnote_markers"], part.get("footnote_markers"))
    _extend_unique(merged["pt_terms"], part.get("pt_terms"))
    _extend_unique(merged["missing_n_rows"], part.get("missing_n_rows"))
    if part.get("notes") and not merged["notes"]:
        merged["notes"] = part["notes"]


def _merge_groups(dst: list, src: list) -> None:
    seen = {_norm(g.get("label", "")): g for g in dst if g.get("label")}
    for g in src or []:
        lab = g.get("label")
        if not lab:
            continue
        k = _norm(lab)
        if k not in seen:
            dst.append(g)
            seen[k] = g
        elif seen[k].get("n") is None and g.get("n") is not None:
            seen[k]["n"] = g.get("n")     # fill a missing big-N from a later page


def _extend_unique(dst: list, src: list) -> None:
    seen = set(dst)
    for x in src or []:
        if x not in seen:
            dst.append(x)
            seen.add(x)


# --------------------------------------------------------------------------- #
# Extraction self-check (Precision guard #1): recompute round(n/N*100) on the
# extracted cells; re-read only the ones that don't reconcile (or where n>N).
# --------------------------------------------------------------------------- #

def suspect_cells(extraction: dict, tol: float = 0.6) -> list[dict]:
    """Pure detector (no I/O). Return the cells whose extracted count/percent are
    internally inconsistent (|round(n/N*100) - pct| > tol) or where n exceeds the
    group N — i.e. the extraction (not the source) is likely mis-read."""
    out: list[dict] = []
    if not extraction:
        return out
    group_n = {_norm(g["label"]): _num(g.get("n")) for g in extraction.get("groups", []) if g.get("label")}
    for r in extraction.get("summary_rows", []) or []:
        vals = r.get("values") or {}
        pcts = r.get("pcts") or {}
        for g, raw in vals.items():
            n = _num(raw)
            N = group_n.get(_norm(g))
            if n is None or N is None or N <= 0:
                continue
            p = _num(pcts.get(g)) if pcts else None
            bad = n > N or (p is not None and abs(round(n / N * 100, 1) - p) > tol)
            if bad:
                out.append({"row_label": r.get("label"), "group": g, "n": n, "N": N, "pct": p})
    return out


_RECHECK_SCHEMA = {
    "type": "object",
    "properties": {"corrections": {"type": "array", "items": {"type": "object", "properties": {
        "row_label": {"type": "string"}, "group": {"type": "string"},
        "n": {"type": ["number", "null"], "description": "the count exactly as printed"},
        "pct": {"type": ["number", "null"], "description": "the percent exactly as printed"},
    }, "required": ["row_label", "group"]}}},
    "required": ["corrections"],
}


def needs_self_check(extraction: dict, config: dict) -> bool:
    """Whether self_check() would actually do anything for this extraction.

    Pure — no PDF, no API. self_check needs the source TEXT only to re-read cells it has
    already decided are suspect, and suspect detection depends on the extraction alone.
    So a caller can ask this first and skip reading the PDF (~0.6 s/page) when the answer
    is no. Same gate self_check applies internally, kept in one place so the two can't
    drift apart.
    """
    sc = (config or {}).get("selfcheck", {}) or {}
    if not sc.get("enabled", True) or not extraction:
        return False
    return bool(suspect_cells(extraction, float(sc.get("pct_tolerance", 0.6))))


def self_check(text: str, extraction: dict, config: dict) -> tuple[dict, int, int]:
    """Re-read the suspect cells against the source text and patch the extraction in
    place. Returns (extraction, n_suspect, n_corrected). One focused model call, only
    when suspects exist, so cost stays flat. Any model or response error propagates:
    an unverified suspect cell must never be treated as a clean review."""
    sc = (config or {}).get("selfcheck", {}) or {}
    if not sc.get("enabled", True) or not extraction:
        return extraction, 0, 0
    suspects = suspect_cells(extraction, float(sc.get("pct_tolerance", 0.6)))
    if not suspects:
        return extraction, 0, 0
    ask = [{"row_label": s["row_label"], "group": s["group"],
            "current_n": s["n"], "current_pct": s["pct"]} for s in suspects]
    user = ("Some extracted cells look mis-read: the count and percent are inconsistent, or the "
            "count exceeds the group N. Re-read ONLY these cells from the source text and return "
            "the count (n) and percent (pct) exactly as printed.\n\n"
            f"Suspect cells: {json.dumps(ask, ensure_ascii=False)}\n\n"
            f"--- OUTPUT TEXT ---\n{text}\n--- END ---")
    data = ai_client.call_structured(
        "You re-read exact numeric cell values from a clinical table for QC. "
        "Copy digits exactly; never compute or round.",
        user, "recheck", _RECHECK_SCHEMA, max_tokens=2000)
    if not isinstance(data, dict) or not isinstance(data.get("corrections"), list):
        raise AIReviewResponseError("self-check returned malformed corrections")
    corr = {(_norm(c.get("row_label", "")), _norm(c.get("group", ""))): c
            for c in data.get("corrections", [])}
    n_applied = 0
    for r in extraction.get("summary_rows", []) or []:
        rk = _norm(r.get("label", ""))
        vals = r.get("values") or {}
        pcts = r.get("pcts")
        if not isinstance(pcts, dict):
            pcts = {}
        for g in list(vals.keys()):
            c = corr.get((rk, _norm(g)))
            if not c:
                continue
            cn = _num(c.get("n"))
            if cn is not None and cn != _num(vals.get(g)):
                vals[g] = c.get("n")
                n_applied += 1
            cp = _num(c.get("pct"))
            if cp is not None:
                pcts[g] = c.get("pct")
        if pcts:
            r["pcts"] = pcts
        r["values"] = vals
    return extraction, len(suspects), n_applied


# --------------------------------------------------------------------------- #
# Checklist-driven AI judges (Layer C) — the model does the judging.
# --------------------------------------------------------------------------- #

# Finding check_id prefix per checklist scope (drives dedupe family + UI scope).
_SCOPE_PREFIX = {"within_table": "AIW", "cross_output": "AIX", "version": "AIV"}

_JUDGE_SYS = (
    "You are a senior statistical reviewer performing QC on clinical-trial TLF outputs. "
    "You are given data already extracted from the table(s); treat those numbers as "
    "authoritative and do the JUDGING. Apply ONLY the checklist items provided. Be precise "
    "and conservative — raise a finding only when the numbers show a genuine discrepancy, "
    "never on a hunch, and never invent numbers. Copy numbers exactly; do not round. Every "
    "numeric finding MUST cite the exact numbers, the operation relating them, and the "
    "observed value it contradicts, so the arithmetic can be machine-verified."
)


def check_id_for(item: dict) -> str:
    """The finding check_id for a checklist/deterministic item. Checklist items get a
    scope prefix (AIW/AIX/AIV) + their id; deterministic items keep their own check_id."""
    scope = item.get("scope")
    if scope in _SCOPE_PREFIX:
        return f"{_SCOPE_PREFIX[scope]}-{item.get('id', '')}"
    return item.get("check_id", "")


def checklist_index(config: dict) -> dict:
    """{check_id -> item} across both the AI checklist and the deterministic checks —
    used by export/UI to resolve a finding's title/risk/scope."""
    idx: dict[str, dict] = {}
    for it in config.get("checklist", []) or []:
        idx[check_id_for(it)] = it
    for it in config.get("deterministic_checks", []) or []:
        idx[it.get("check_id", "")] = it
    return idx


def _items_for_scope(config: dict, scopes: set[str]) -> list[dict]:
    return [it for it in (config.get("checklist", []) or []) if it.get("scope") in scopes]


def _applies(item: dict, label: str, title: str) -> bool:
    pats = item.get("applies_to") or ["*"]
    hay = f"{label} {title}".lower()
    for p in pats:
        p = (p or "").strip().lower()
        if p in ("*", ""):
            return True
        core = p.strip("*")
        if core and core in hay:
            return True
    return False


def _render_items(items: list[dict]) -> str:
    lines: list[str] = []
    for it in items:
        lines.append(f"[{it['id']}] ({it.get('risk', '')} risk) {it.get('title', '')}: {it.get('guidance', '')}")
        for ex in it.get("examples") or []:
            lines.append(f"    example: {ex}")
    return "\n".join(lines)


def _judge_schema(cross: bool) -> dict:
    props = {
        "checklist_item": {"type": "string", "description": "the checklist item id this finding is for, e.g. '2.1'"},
        "risk": {"type": "string", "enum": ["High", "Low"]},
        "message": {"type": "string", "description": "one sentence naming the rows/columns/tables and the numbers involved"},
        "cited_numbers": {"type": "array", "items": {"type": "number"},
            "description": "the exact numbers your claim depends on, ordered as the operation implies"},
        "operation": {"type": "string", "enum": ["sum_equals", "equals", "less_equal", "decreased", "increased", "none"],
            "description": "sum_equals: cited_numbers are addends that should equal `observed`. equals: cited[0] should equal cited[1]. less_equal: cited[0] should be <= cited[1]. decreased/increased: cited=[prior,current]. none: qualitative, no arithmetic."},
        "observed": {"type": ["number", "null"], "description": "the printed value your claim contradicts (e.g. the printed total for sum_equals)"},
        "page": {"type": ["integer", "null"]},
        "printed_page": {"type": ["integer", "null"]},
        "pages_total": {"type": ["integer", "null"]},
        "section": {"type": ["string", "null"]},
        "row_kind": {"type": ["string", "null"], "description": "'study' or 'aggregate'"},
        "subjects": {"type": "array", "items": {"type": "string"}, "description": "study/subject IDs the finding is about"},
    }
    if cross:
        props["affected"] = {"type": "array", "items": {"type": "string"},
            "description": "labels of the outputs this finding compares, e.g. ['Table 1','Table 2.1']"}
    return {"type": "object",
            "properties": {"findings": {"type": "array", "items": {
                "type": "object", "properties": props,
                "required": ["checklist_item", "risk", "message", "operation"]}}},
            "required": ["findings"]}


# The app has ONE two-tier scale: risk ∈ {High, Low}. "Medium" (and anything unknown)
# reads as Low — which is how Medium was already displayed. `severity` is kept as the
# lowercased tier purely as a sort/group key and legacy column; it mirrors the risk.
def _norm_risk(risk: str) -> str:
    return "High" if (risk or "").strip().lower() == "high" else "Low"


def _sev(risk: str) -> str:
    return _norm_risk(risk).lower()   # "high" | "low"


def _build_judge_findings(raw: list, config: dict, id2: dict,
                          default_scope: str, cross: bool = False) -> list[dict]:
    out: list[dict] = []
    # A valid clean response is exactly a list with zero entries. Anything else is
    # an AI-stage failure and must propagate so the runner withholds auto-approval.
    if not isinstance(raw, list):
        raise AIReviewResponseError("judge response field 'findings' is not a list")
    for r in raw:
        if not isinstance(r, dict):
            raise AIReviewResponseError("judge response contains a non-object finding")
        cid = str(r.get("checklist_item", "")).strip()
        if not cid or cid not in id2:
            raise AIReviewResponseError(f"judge returned unknown checklist item: {cid or '<blank>'}")
        item = id2[cid]
        scope = item.get("scope", default_scope)
        risk = r.get("risk") or item.get("risk") or ("High" if scope != "version" else "Low")
        op = r.get("operation") or "none"
        nums = [n for n in (r.get("cited_numbers") or [])
                if isinstance(n, (int, float)) and not isinstance(n, bool)]
        obs = r.get("observed")
        obs_num = obs if isinstance(obs, (int, float)) and not isinstance(obs, bool) else None
        numbers = list(nums)
        if op == "sum_equals" and obs_num is not None:
            numbers = nums + [obs_num]
        subjects = r.get("subjects") or _subject_ids(r.get("message", ""))
        f = {
            "check_id": f"{_SCOPE_PREFIX.get(scope, 'AI')}-{item.get('id', cid)}",
            "checklist_item": item.get("id", cid), "risk": _norm_risk(risk), "severity": _sev(risk),
            "message": r.get("message", ""), "operation": op,
            "cited_numbers": nums, "observed": obs_num,
            "numbers": numbers, "subjects": subjects,
            "page": r.get("page"), "printed_page": r.get("printed_page"),
            "pages_total": r.get("pages_total"), "section": r.get("section"),
            "row_kind": r.get("row_kind"),
        }
        if cross:
            f["affected"] = r.get("affected") or []
        out.append(f)
    return out


def within_table_judge(extraction: dict, prior: dict | None, label: str, title: str,
                       config: dict) -> list[dict]:
    """One model call per table: within-table checklist (2.1 sums, 2.2 row sums, 2.3
    integrity, 7.2 AE-overview zeros) plus — when a prior extraction is supplied — the
    version items (6.1/6.2/6.3, 7.3/7.4) folded into the same call."""
    if not extraction:
        return []
    items = [it for it in _items_for_scope(config, {"within_table"}) if _applies(it, label, title)]
    if prior:
        items += [it for it in _items_for_scope(config, {"version"}) if _applies(it, label, title)]
    if not items:
        return []
    id2 = {it["id"]: it for it in items}
    payload = {"label": label, "title": title, "groups": extraction.get("groups", []),
               "summary_rows": extraction.get("summary_rows", []),
               "footnote_markers": extraction.get("footnote_markers", [])}
    user = (f"TABLE UNDER REVIEW: {label} — {title}\n\n"
            f"Extracted data (numbers copied from the table):\n"
            f"{json.dumps(payload, ensure_ascii=False)}\n\n")
    if prior:
        user += ("Prior-edition extracted data (for the version items):\n"
                 f"{json.dumps(_compact(prior), ensure_ascii=False)}\n\n")
    user += ("Apply ONLY the checklist items below. Report a finding only for a real discrepancy; "
             "if everything reconciles, return an empty list. For every numeric finding cite the "
             "exact numbers + operation, and copy the page/section/row_kind of the row you flag.\n\n"
             f"CHECKLIST:\n{_render_items(items)}")
    data = ai_client.call_structured(_JUDGE_SYS, user, "within_table_findings",
                                     _judge_schema(False), max_tokens=6000)
    if not isinstance(data, dict) or "findings" not in data:
        raise AIReviewResponseError("within-table judge returned malformed structured output")
    raw = data["findings"]
    return _build_judge_findings(raw, config, id2, default_scope="within_table")


def cross_output_judge(bundle: list[dict], config: dict) -> list[dict]:
    """One model call over the whole delivery: cross-output checklist (3 pooled, 4
    by-study, 5 footnotes, 7.1 overview↔SOC/PT, 8 by-study↔Table 1). `bundle` is the
    list of per-output compact extractions. cross_document items are gated out."""
    items = [it for it in _items_for_scope(config, {"cross_output"})
             if not it.get("requires_multi_document_upload")]
    if not items or not bundle:
        return []
    id2 = {it["id"]: it for it in items}
    user = ("You are reconciling numbers ACROSS multiple TLF outputs from one delivery. Below is the "
            "extracted data for each output (Table 1 is the exposure/grouping hub).\n\n"
            f"{json.dumps(bundle, ensure_ascii=False)}\n\n"
            "Apply ONLY the checklist items below. Report a finding only for a real cross-output "
            "discrepancy. If a study is deliberately summarized separately (see the guidance/examples), "
            "do NOT flag it. Cite exact numbers + operation, list the `affected` output labels, and give "
            "the page/section in the primary output when known.\n\n"
            f"CHECKLIST:\n{_render_items(items)}")
    data = ai_client.call_structured(_JUDGE_SYS, user, "cross_output_findings",
                                     _judge_schema(True), max_tokens=8000)
    if not isinstance(data, dict) or "findings" not in data:
        raise AIReviewResponseError("cross-output judge returned malformed structured output")
    raw = data["findings"]
    return _build_judge_findings(raw, config, id2,
                                 default_scope="cross_output", cross=True)


_KEYROW_RE = re.compile(
    r"any (adverse|ae|treatment.?emergent)|teae|^\s*total\b|overall|all subjects|"
    r"received at least|subjects who received|number of subjects|serious|death|grade", re.I)


def _compact(ex: dict, max_rows: int | None = None) -> dict:
    """Projection of one extraction for the cross-output bundle.

    EVERY body row is included by default. The previous behaviour kept only rows matching
    _KEYROW_RE, which on real deliveries selected 1-5 rows per table — a 295-row SOC/PT
    table contributed a single row, so checklist item 7.1 (AE overview categories match
    SOC/PT) had almost nothing to reconcile against. A row the judge never sees cannot be
    reconciled, so the cap was silently limiting what cross-output checking could find.

    `pcts` and `row_kind` are dropped: items 3/4/5/7.1/8 compare COUNTS and group N's, not
    percentages, and those two fields were the bulk of the bytes.

    Pass `max_rows` to restore the old keyrow-only cap (kept for callers that need a
    genuinely small projection).
    """
    if not ex:
        return {}
    rows = ex.get("summary_rows", []) or []
    if max_rows is not None:
        key = [r for r in rows if _KEYROW_RE.search(r.get("label", "") or "")][:max_rows]
        rows = key or rows[:max_rows]

    def _proj(r: dict) -> dict:
        out = {"label": r.get("label"), "values": r.get("values")}
        if r.get("section"):
            out["section"] = r["section"]
        if r.get("page") is not None:
            out["page"] = r["page"]
        return out

    return {
        "groups": ex.get("groups", []),
        "footnote_markers": ex.get("footnote_markers", []),
        "rows": [_proj(r) for r in rows],
    }


# --------------------------------------------------------------------------- #
# Verify pass (Precision guard #2): re-check the arithmetic each finding cites.
# --------------------------------------------------------------------------- #

def _contradicts(op: str, nums: list, obs, tol: float):
    """True = the cited numbers really contradict the claim (keep the finding);
    False = they are self-consistent (an LLM math slip → drop); None = not enough
    information to verify (keep, don't silently suppress a possibly-real finding)."""
    if op == "none":
        return None
    if op == "sum_equals":
        if not nums or obs is None:
            return None
        return abs(sum(nums) - obs) > tol
    if op == "equals":
        if len(nums) < 2:
            return None
        return abs(nums[0] - nums[1]) > tol
    if op == "less_equal":
        if len(nums) < 2:
            return None
        return nums[0] - nums[1] > tol            # claim a<=b is violated iff a>b
    if op == "decreased":
        if len(nums) < 2:
            return None
        return nums[1] < nums[0] - tol            # cited=[prior,current]; decreased iff current<prior
    if op == "increased":
        if len(nums) < 2:
            return None
        return nums[1] > nums[0] + tol
    return None


def verify_findings(findings: list[dict], config: dict) -> tuple[list[dict], int]:
    """Drop numeric findings whose own cited arithmetic does not actually hold. Returns
    (kept, n_dropped). Qualitative findings (operation 'none') always pass through."""
    vp = (config or {}).get("verify_pass", {}) or {}
    if not vp.get("enabled", True):
        return findings, 0
    tol = float(vp.get("count_tolerance", 0))
    kept: list[dict] = []
    dropped = 0
    for f in findings:
        nums = [n for n in (f.get("cited_numbers") or [])
                if isinstance(n, (int, float)) and not isinstance(n, bool)]
        obs = f.get("observed")
        obs = obs if isinstance(obs, (int, float)) and not isinstance(obs, bool) else None
        verdict = _contradicts(f.get("operation") or "none", nums, obs, tol)
        if verdict is False:
            dropped += 1
            continue
        kept.append(f)
    return kept, dropped


# --------------------------------------------------------------------------- #
# Non-numeric within-table findings (model flagged during extraction; formatted here)
# --------------------------------------------------------------------------- #

def within_table_findings(extraction: dict, label: str, config: dict) -> list[dict]:
    findings: list[dict] = []
    for r in extraction.get("missing_n_rows") or []:
        findings.append({
            "check_id": "FMT-002", "severity": "low", "risk": "Low",
            "message": f"Row '{r}' is missing an N where one is expected.",
            "subjects": [], "numbers": [], "page": None,
        })
    return findings


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


_SUBJ_RE = re.compile(r"\b([A-Z]{1,3}\d{2,}-?\w*|\d{6,}[A-Z]{2,}\d*)\b")


def _subject_ids(text: str) -> list[str]:
    return list(dict.fromkeys(_SUBJ_RE.findall(text or "")))
