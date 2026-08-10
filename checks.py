"""Deterministic STRUCTURAL checks (no LLM) + finding de-duplication.

After the AI-review inversion these are the only Python checks that produce findings:
  * FMT-010  blank/empty pages           (checklist 1.1)
  * XOUT-020 outputs missing vs prior     (checklist 1.2)
  * XOUT-001 gaps in output numbering      (checklist 1.3)
All numeric / cross-output / version judging moved to the AI judges in ai_review.py.

`finding_signature` builds the stable key stored on every finding so a future step can
match "the same finding" across runs (and against the human-review log); `dedupe`
collapses overlapping findings within a single run.
"""

from __future__ import annotations

import re

import pdftools


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


# The check_id families produced by the deterministic structural checks in this module
# (FMT-010, XOUT-020, XOUT-001). Used to recognise these on Excel re-import so the app's
# own checks — already generated at project creation — are not duplicated by the copies a
# round-tripped export carries. AI findings use different families (AIW/AIX/AIV).
DETERMINISTIC_FAMILIES = frozenset({"FMT", "XOUT"})


def is_deterministic_check(check_id) -> bool:
    """True when check_id belongs to a deterministic structural check (not an AI finding)."""
    return str(check_id or "").split("-")[0].strip().upper() in DETERMINISTIC_FAMILIES


# --------------------------------------------------------------------------- #
# 1.1 / FMT-010: blank / empty pages (deterministic, whole document)
# --------------------------------------------------------------------------- #

def blank_page_findings(doc_path: str, outputs: list[dict], min_chars: int = 3) -> list[dict]:
    """Flag pages whose text is effectively empty, attributing each to the output
    whose page range contains it. Uses a fast per-page char count (pypdfium2)."""
    findings: list[dict] = []
    counts = pdftools.page_char_counts(doc_path)
    for i, c in enumerate(counts):
        if c < min_chars:
            page = i + 1
            owner = next((o for o in outputs if o["page_start"] <= page <= o["page_end"]), None)
            findings.append({
                "check_id": "FMT-010", "severity": "low", "risk": "Low",
                "message": f"Page {page} appears blank/empty"
                           + (f" (within {owner['label']})." if owner else "."),
                "subjects": [], "numbers": [], "page": page,
                "_output_label": owner["label"] if owner else None,
            })
    return findings


# --------------------------------------------------------------------------- #
# 1.2 / XOUT-020: outputs present in prior edition but missing in current
# --------------------------------------------------------------------------- #

def missing_output_findings(current_labels: list[str], prior_labels: list[str]) -> list[dict]:
    cur = {_norm(l) for l in current_labels}
    findings = []
    for pl in prior_labels:
        if _norm(pl) not in cur:
            findings.append({
                "check_id": "XOUT-020", "severity": "low", "risk": "Low",
                "message": f"'{pl}' was present in the prior edition but is missing in the current edition.",
                "subjects": [], "numbers": [], "page": None, "affected": [pl],
            })
    return findings


# --------------------------------------------------------------------------- #
# 1.3 / XOUT-001: numbering gaps in the output index
# --------------------------------------------------------------------------- #

def toc_gap_findings(numbers: list[str]) -> list[dict]:
    """Given output numbers like ['1','2.1','4.3','4.5'], flag missing siblings.

    Groups by the prefix (all components but the last); within a group the last
    components should be a contiguous 1..max run. Missing integers are reported.
    """
    all_numbers = [n for n in numbers if n]

    def present_at(base: str) -> bool:
        # base "4.4" counts as present if 4.4 exists OR any deeper number (4.4.1, …) does.
        return any(n == base or n.startswith(base + ".") for n in all_numbers)

    groups: dict[str, set[int]] = {}
    for num in numbers:
        parts = [p for p in num.split(".") if p != ""]
        if not parts or not parts[-1].isdigit():
            continue
        prefix = ".".join(parts[:-1])
        groups.setdefault(prefix, set()).add(int(parts[-1]))
    findings: list[dict] = []
    for prefix, present in groups.items():
        if len(present) < 2:
            continue
        lo, hi = min(present), max(present)
        missing = [i for i in range(lo, hi + 1) if i not in present]
        for m in missing:
            base = f"{prefix}.{m}" if prefix else str(m)
            before = f"{prefix}.{m-1}" if prefix else str(m - 1)
            after = f"{prefix}.{m+1}" if prefix else str(m + 1)
            if present_at(base):   # exists as a parent of deeper-numbered outputs
                continue
            findings.append({
                # A gap means a whole output may be absent — kept in the High tier, as it
                # displayed before (was severity "major"). The other structural checks are Low.
                "check_id": "XOUT-001", "severity": "high", "risk": "High",
                "message": (f"Numbering skips from Table {before} to Table {after} "
                            f"without a Table {base}."),
                "subjects": [], "numbers": [], "page": None,
                "affected": [f"Table {before}", f"Table {after}"],
            })
    return findings


# --------------------------------------------------------------------------- #
# Finding signature + de-duplication
# --------------------------------------------------------------------------- #

def finding_signature(check_id: str, output_label, numbers, message: str) -> str:
    """A stable identity for a finding: check family + output + sorted rounded
    numbers + a normalized message stub. Stored on every finding (inert at runtime,
    since findings are cleared each run) so a later step can recognise the same
    finding across runs and correlate it with the human-review log."""
    nums = tuple(sorted(round(float(n), 3) for n in (numbers or [])
                        if isinstance(n, (int, float)) and not isinstance(n, bool)))
    family = (check_id or "").split("-")[0]
    return "|".join([family, _norm(output_label or ""), repr(nums), _norm(message or "")[:60]])


def dedupe(findings: list[dict]) -> list[dict]:
    """Drop near-duplicate findings that describe the same cell/issue. Keeps the
    first (higher-priority) occurrence. Signature = check family + page + sorted
    numbers + a normalized message stub."""
    seen = set()
    out = []
    for f in findings:
        nums = tuple(sorted(round(float(n), 3) for n in (f.get("numbers") or []) if isinstance(n, (int, float))))
        sig = (f.get("check_id", "").split("-")[0], f.get("page"), nums,
               _norm(f.get("message", ""))[:60])
        if sig in seen:
            continue
        seen.add(sig)
        out.append(f)
    return out
