"""Deterministic generation of fictional, internally consistent TLF opportunities.

The public case records contain only observable evidence.  Issue labels are kept
in a separate truth stream so predictors can be run on ``cases.jsonl`` without
receiving the answers.
"""

from __future__ import annotations

import hashlib
import random
from typing import Any

from .catalog import BENCHMARK_SEED, FAMILY_BY_ID, FAMILIES


def stable_int(*parts: object) -> int:
    payload = "|".join(str(p) for p in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def stable_uniform(*parts: object) -> float:
    return stable_int(*parts) / float(2**64 - 1)


def positive_project_indices(n_projects: int, family_index: int,
                             positives_per_family: int) -> set[int]:
    """Select distinct projects for one family without relying on global RNG state."""
    if positives_per_family > n_projects:
        raise ValueError("positives_per_family cannot exceed n_projects")
    # Pick a step coprime to common benchmark sizes, falling back until it spans
    # all indices.  The offset differs by family, spreading defects across projects.
    step = 7
    while len({(j * step) % n_projects for j in range(n_projects)}) != n_projects:
        step += 2
    start = (family_index * 11) % n_projects
    return {(start + j * step) % n_projects for j in range(positives_per_family)}


def _evidence(family_id: str, positive: bool, rng: random.Random) -> dict[str, Any]:
    """Create observable evidence that either satisfies or violates one rule."""
    n = rng.randint(70, 240)
    a = rng.randint(15, max(16, n // 2))
    b = rng.randint(10, max(11, n // 3))

    if family_id == "FMT-010":
        return {"character_count": 0 if positive else rng.randint(240, 900)}
    if family_id == "XOUT-020":
        return {"prior_present": True, "current_present": not positive,
                "prior_label": "Table 5.2", "current_labels": ["Table 5.1", "Table 5.2"] if not positive else ["Table 5.1", "Table 5.3"]}
    if family_id == "XOUT-001":
        return {"numbers": ["4.1", "4.2", "4.4"] if positive else ["4.1", "4.2", "4.3"],
                "expected": "4.3"}
    if family_id in {"AIW-2.1", "AIW-2.2"}:
        total = a + b + (rng.choice([-3, -2, 2, 3]) if positive else 0)
        return {"addends": [a, b], "printed_total": total}
    if family_id == "AIW-2.3":
        # The benchmark family covers the central n<=N numerical-integrity rule.
        count = n + rng.randint(1, 7) if positive else n - rng.randint(1, 12)
        return {"count": count, "group_n": n}
    if family_id in {"AIX-3", "AIX-4", "AIX-7.1", "AIX-8"}:
        left = n
        right = n + rng.choice([-6, -4, 4, 6]) if positive else n
        return {"left": left, "right": right}
    if family_id == "AIX-5":
        expected = ["SYN-101", "SYN-102", "SYN-103"]
        observed = expected[:-1] + ["SYN-199"] if positive else list(expected)
        return {"expected_set": expected, "observed_set": observed}
    if family_id == "AIV-6.1":
        return {"prior_present": not positive, "current_present": True,
                "study": "SYN-NEW-01"}
    if family_id in {"AIV-6.2", "AIV-7.3"}:
        prior = n
        current = n - rng.randint(2, 9) if positive else n + rng.randint(0, 8)
        return {"prior": prior, "current": current}
    if family_id == "AIV-6.3":
        prior = n
        current = n + rng.randint(2, 9) if positive else n
        return {"prior": prior, "current": current}
    if family_id == "AIW-7.2":
        return {"any_ae_count": 0 if positive else rng.randint(1, max(2, n // 2)),
                "group_n": n}
    if family_id == "AIV-7.4":
        if positive and rng.random() < 0.5:
            return {"prior_term": "Injection site pain", "current_term": "Administration site pain",
                    "prior_soc": "General disorders", "current_soc": "General disorders"}
        return {"prior_term": "Injection site pain", "current_term": "Injection site pain",
                "prior_soc": "General disorders",
                "current_soc": "Musculoskeletal disorders" if positive else "General disorders"}
    raise KeyError(family_id)


def evidence_is_violation(opportunity: dict) -> bool:
    """Infer the label from observable evidence, without reading the truth manifest."""
    op = opportunity["operation"]
    e = opportunity["evidence"]
    if op == "blank":
        return int(e["character_count"]) < 3
    if op == "missing_current":
        return bool(e["prior_present"]) and not bool(e["current_present"])
    if op == "number_gap":
        return e["expected"] not in set(e["numbers"])
    if op == "sum_equals":
        return sum(e["addends"]) != e["printed_total"]
    if op == "less_equal":
        return e["count"] > e["group_n"]
    if op == "equals":
        return e["left"] != e["right"]
    if op == "set_equals":
        return set(e["expected_set"]) != set(e["observed_set"])
    if op == "new_present":
        return not bool(e["prior_present"]) and bool(e["current_present"])
    if op == "decreased":
        return e["current"] < e["prior"]
    if op == "increased":
        return e["current"] > e["prior"]
    if op == "zero_forbidden":
        return e["any_ae_count"] == 0
    if op == "term_changed":
        return (e["prior_term"] != e["current_term"] or
                e["prior_soc"] != e["current_soc"])
    raise KeyError(op)


def _locator(project_index: int, family_index: int) -> dict[str, Any]:
    table_no = (family_index * 3 + project_index) % 10 + 1
    compare_no = table_no % 10 + 1
    return {
        "output_label": f"Table {table_no}",
        "page": table_no,
        "row": f"SYNTHETIC ROW {family_index + 1}",
        "column": "Drug X",
        "comparison_output": f"Table {compare_no}",
    }


def _pages(project_index: int) -> list[dict[str, Any]]:
    pages = []
    for role in ("current", "prior"):
        for page in range(1, 11):
            pages.append({
                "document_role": role,
                "page": page,
                "table_label": f"Table {page}",
                "title": f"Synthetic aggregate summary {page}",
                "character_count": 320 + ((project_index * 37 + page * 19) % 480),
                "one_table_per_page": True,
                "watermark": "SYNTHETIC — NOT FOR CLINICAL USE",
            })
    return pages


def _extraction_units(project_id: str, project_index: int) -> list[dict[str, Any]]:
    rows = []
    for table_no in range(1, 11):
        for row_no in range(1, 6):
            difficulty = ("hard" if (project_index + table_no + row_no) % 11 == 0
                          else "medium" if (project_index + table_no + row_no) % 5 == 0
                          else "easy")
            rows.append({
                "row_id": f"{project_id}:T{table_no}:R{row_no}",
                "output_label": f"Table {table_no}",
                "difficulty": difficulty,
                "numeric_cells": 4,
                "checkable_cells": 2,
            })
    return rows


def generate_dataset(n_projects: int = 50, positives_per_family: int = 10,
                     seed: int = BENCHMARK_SEED) -> tuple[list[dict], list[dict]]:
    """Return ``(public_cases, private_truth)`` in stable order."""
    positive_sets = [positive_project_indices(n_projects, i, positives_per_family)
                     for i in range(len(FAMILIES))]
    cases: list[dict] = []
    truth: list[dict] = []
    for pidx in range(n_projects):
        pid = f"SYN-P{pidx + 1:03d}"
        pages = _pages(pidx)
        opportunities = []
        for fidx, family in enumerate(FAMILIES):
            positive = pidx in positive_sets[fidx]
            rng = random.Random(stable_int(seed, pid, family.id, "evidence"))
            evidence = _evidence(family.id, positive, rng)
            locator = _locator(pidx, fidx)
            oid = f"{pid}:{family.id}"
            opp = {
                "opportunity_id": oid,
                "project_id": pid,
                "family": family.id,
                "title": family.title,
                "risk": family.risk,
                "scope": family.scope,
                "operation": family.operation,
                "detector_group": family.detector_group,
                "difficulty": ("hard" if (pidx + fidx) % 9 == 0
                               else "medium" if (pidx + fidx) % 4 == 0 else "easy"),
                "coverage_complete": (stable_uniform(seed, oid, "coverage") >= 0.025),
                "locator": locator,
                "evidence": evidence,
            }
            opportunities.append(opp)
            if positive:
                truth.append({
                    "truth_id": f"TRUTH:{oid}",
                    "opportunity_id": oid,
                    "project_id": pid,
                    "family": family.id,
                    "risk": family.risk,
                    "locator": locator,
                    "evidence": evidence,
                })
                if family.id == "FMT-010":
                    # Keep the page inventory faithful to the planted blank page.
                    page = next(pg for pg in pages if pg["document_role"] == "current"
                                and pg["page"] == locator["page"])
                    page["character_count"] = 0
        cases.append({
            "project_id": pid,
            "compound": f"Synthetic Compound {chr(65 + pidx % 26)}-{pidx + 1:02d}",
            "study": f"SYN-STUDY-{pidx + 1:03d}",
            "current_edition": "Synthetic 2026",
            "prior_edition": "Synthetic 2025",
            "pages": pages,
            "opportunities": opportunities,
            "extraction_units": _extraction_units(pid, pidx),
        })
    return cases, truth


def validate_dataset(cases: list[dict], truth: list[dict], *, n_projects: int,
                     positives_per_family: int) -> None:
    """Fail loudly if a generated benchmark violates its published data card."""
    if len(cases) != n_projects:
        raise AssertionError(f"expected {n_projects} projects, got {len(cases)}")
    if sum(len(c["pages"]) for c in cases) != n_projects * 20:
        raise AssertionError("each project must contain 10 current and 10 prior pages")
    if any(not p["one_table_per_page"] for c in cases for p in c["pages"]):
        raise AssertionError("every synthetic page must contain exactly one table")
    counts = {f.id: 0 for f in FAMILIES}
    for t in truth:
        counts[t["family"]] += 1
    expected = {f.id: positives_per_family for f in FAMILIES}
    if counts != expected:
        raise AssertionError(f"truth family counts differ: {counts}")
    # Independent evidence evaluation catches generator/label drift.
    by_id = {o["opportunity_id"]: o for c in cases for o in c["opportunities"]}
    truth_ids = {t["opportunity_id"] for t in truth}
    observed_positive = {oid for oid, o in by_id.items() if evidence_is_violation(o)}
    if observed_positive != truth_ids:
        raise AssertionError("truth manifest and observable evidence disagree")
