"""Deterministic baselines and a seeded simulated-LLM behavioral proxy."""

from __future__ import annotations

import json
import re
from typing import Any

from .catalog import (FAMILY_BY_ID, NUMERIC_OPERATIONS, SIMULATOR_VERSION,
                      STRUCTURAL_FAMILIES)
from .generate import evidence_is_violation, opportunity_difficulty, stable_uniform

RULES_ONLY = "rules_only"
LLM_ONLY = "llm_only_simulated"
HYBRID = "hybrid_simulated"
SYSTEMS = (RULES_ONLY, LLM_ONLY, HYBRID)

DETECTION_PROBABILITY = {
    "structural": 0.72,
    "arithmetic": 0.87,
    "cross_output": 0.78,
    "version": 0.82,
    "semantic": 0.74,
}
FALSE_POSITIVE_PROBABILITY = {
    "structural": 0.030,
    "arithmetic": 0.026,
    "cross_output": 0.042,
    "version": 0.032,
    "semantic": 0.060,
}
DIFFICULTY_MULTIPLIER = {"easy": 1.0, "medium": 0.91, "hard": 0.77}


def _prediction_key_fields(opp: dict) -> dict:
    family = FAMILY_BY_ID[opp["family"]]
    return {
        "project_id": opp["project_id"],
        "family": opp["family"],
        "risk": family.risk,
        "locator": dict(opp["locator"]),
    }


def _citation(opp: dict, *, corrupt: bool = False) -> tuple[str, list[float], float | None]:
    op = FAMILY_BY_ID[opp["family"]].operation
    e = opp["evidence"]
    if op == "sum_equals":
        nums = list(e["addends"])
        observed = sum(nums) if corrupt else e["printed_total"]
        return "sum_equals", nums, observed
    if op == "less_equal":
        nums = [e["count"], e["count"] if corrupt else e["group_n"]]
        return "less_equal", nums, None
    if op == "equals":
        nums = [e["left"], e["left"] if corrupt else e["right"]]
        return "equals", nums, None
    if op in {"decreased", "increased"}:
        nums = [e["prior"], e["prior"] if corrupt else e["current"]]
        return op, nums, None
    # The production judge schema uses operation='none' for qualitative/set, presence, blank-page, numbering, zero, and terminology findings.
    return "none", [], None


def _make_prediction(opp: dict, system: str, ordinal: int, *, seed: int,
                     duplicate: bool = False) -> dict:
    family = FAMILY_BY_ID[opp["family"]]
    is_issue = evidence_is_violation(opp)
    corrupt = (is_issue and family.operation in NUMERIC_OPERATIONS and
               stable_uniform(seed, opp["opportunity_id"], "citation") < 0.04)
    operation, cited, observed = _citation(opp, corrupt=corrupt)
    confidence_base = (DETECTION_PROBABILITY[family.detector_group]
                       if is_issue else 0.52)
    jitter = (stable_uniform(seed, opp["opportunity_id"], "confidence") - 0.5) * 0.16
    message = (f"{family.title} at {opp['locator']['output_label']} / "
               f"{opp['locator']['row']}; review the cited synthetic evidence.")
    numbers = list(cited)
    if operation == "sum_equals" and observed is not None:
        numbers.append(observed)
    return {
        "prediction_id": f"{system}:{opp['opportunity_id']}:{ordinal}",
        **_prediction_key_fields(opp),
        "message": message,
        "operation": operation,
        "cited_numbers": cited,
        "observed": observed,
        "numbers": numbers,
        "confidence": round(max(0.01, min(0.99, confidence_base + jitter)), 4),
        "simulator_version": SIMULATOR_VERSION if system != RULES_ONLY else None,
        "duplicate": duplicate,
        "citation_corrupted": corrupt,
    }


def structural_rules(case: dict, seed: int) -> list[dict]:
    """Current rules-only baseline: exactly the three structural families."""
    predictions = []
    for opp in case["opportunities"]:
        if opp["family"] in STRUCTURAL_FAMILIES and evidence_is_violation(opp):
            predictions.append(_make_prediction(opp, RULES_ONLY, 1, seed=seed))
    return predictions


def simulated_llm_raw(case: dict, seed: int) -> list[dict]:
    """Seeded behavior stub. It is intentionally not presented as a real model."""
    predictions = []
    for opp in case["opportunities"]:
        family = FAMILY_BY_ID[opp["family"]]
        difficulty = opportunity_difficulty(opp)
        is_issue = evidence_is_violation(opp)
        if is_issue:
            probability = (DETECTION_PROBABILITY[family.detector_group]
                           * DIFFICULTY_MULTIPLIER[difficulty])
            emit = stable_uniform(seed, opp["opportunity_id"], "detect") < probability
        else:
            probability = FALSE_POSITIVE_PROBABILITY[family.detector_group]
            if difficulty == "hard":
                probability *= 1.45
            elif difficulty == "medium":
                probability *= 1.18
            emit = stable_uniform(seed, opp["opportunity_id"], "false_positive") < probability
        if not emit:
            continue
        predictions.append(_make_prediction(opp, LLM_ONLY, 1, seed=seed))
        # Identical restatement: one-to-one scoring counts it as an extra FP unless the hybrid's production-style dedupe removes it.
        if stable_uniform(seed, opp["opportunity_id"], "duplicate") < 0.09:
            predictions.append(_make_prediction(opp, LLM_ONLY, 2, seed=seed,
                                                duplicate=True))
    return predictions


def _contradicts(operation: str, numbers: list, observed, tolerance: float = 0.0):
    """Same arithmetic contract as ``ai_review._contradicts``."""
    if operation == "none":
        return None
    if operation == "sum_equals":
        if not numbers or observed is None:
            return None
        return abs(sum(numbers) - observed) > tolerance
    if operation == "equals":
        if len(numbers) < 2:
            return None
        return abs(numbers[0] - numbers[1]) > tolerance
    if operation == "less_equal":
        if len(numbers) < 2:
            return None
        return numbers[0] - numbers[1] > tolerance
    if operation == "decreased":
        if len(numbers) < 2:
            return None
        return numbers[1] < numbers[0] - tolerance
    if operation == "increased":
        if len(numbers) < 2:
            return None
        return numbers[1] > numbers[0] + tolerance
    return None


def verify_predictions(predictions: list[dict]) -> tuple[list[dict], int]:
    kept = []
    dropped = 0
    for pred in predictions:
        verdict = _contradicts(pred.get("operation", "none"),
                               pred.get("cited_numbers", []), pred.get("observed"), 0.0)
        if verdict is False:
            dropped += 1
        else:
            kept.append(pred)
    return kept, dropped


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def dedupe_predictions(predictions: list[dict]) -> tuple[list[dict], int]:
    """Production-style family/page/numbers/message-stub deduplication."""
    seen = set()
    out = []
    for pred in predictions:
        numbers = tuple(sorted(round(float(n), 3) for n in pred.get("numbers", [])
                               if isinstance(n, (int, float)) and not isinstance(n, bool)))
        sig = (pred.get("family", "").split("-")[0],
               pred.get("locator", {}).get("page"), numbers,
               _norm(pred.get("message", ""))[:60])
        if sig in seen:
            continue
        seen.add(sig)
        out.append(pred)
    return out, len(predictions) - len(out)


def _simulate_extraction(case: dict, seed: int, *, self_check: bool) -> dict:
    rows_total = rows_extracted = numeric_total = numeric_exact = recovered = 0
    base_row = {"easy": 0.975, "medium": 0.94, "hard": 0.88}
    base_cell = {"easy": 0.985, "medium": 0.96, "hard": 0.90}
    for row in case["extraction_units"]:
        rows_total += 1
        rid = row["row_id"]
        if stable_uniform(seed, rid, "row_extracted") >= base_row[row["difficulty"]]:
            continue
        rows_extracted += 1
        for cell in range(row["numeric_cells"]):
            numeric_total += 1
            exact = stable_uniform(seed, rid, "numeric_cell", cell) < base_cell[row["difficulty"]]
            if not exact and self_check and cell < row["checkable_cells"]:
                if stable_uniform(seed, rid, "self_check", cell) < 0.82:
                    exact = True
                    recovered += 1
            numeric_exact += int(exact)
    return {
        "rows_total": rows_total,
        "rows_extracted": rows_extracted,
        "numeric_cells_evaluated": numeric_total,
        "numeric_cells_exact": numeric_exact,
        "cells_recovered_by_self_check": recovered,
    }


def _operational_stats(case: dict, predictions: list[dict], seed: int,
                       *, self_check: bool, coverage_gate: bool) -> dict:
    pid = case["project_id"]
    extraction = _simulate_extraction(case, seed, self_check=self_check)
    current_pages = [p for p in case["pages"] if p["document_role"] == "current"]
    prior_pages = [p for p in case["pages"] if p["document_role"] == "prior"]
    read_current = [p for p in current_pages
                    if stable_uniform(seed, pid, "page_read", "current", p["page"]) >= 0.018]
    read_prior = [p for p in prior_pages
                  if stable_uniform(seed, pid, "page_read", "prior", p["page"]) >= 0.018]
    incomplete_current_tables = [p["table_label"] for p in current_pages if p not in read_current]
    payload_chars = len(json.dumps(case["opportunities"], sort_keys=True, separators=(",", ":")))
    input_tokens = 1800 + (payload_chars + 3) // 4
    output_tokens = 70 + 42 * len(predictions)
    # Deterministic proxy latency, never represented as measured wall-clock time.
    latency_s = 0.0
    for opp in case["opportunities"]:
        detector_group = FAMILY_BY_ID[opp["family"]].detector_group
        base = {"structural": 0.22, "arithmetic": 0.48, "cross_output": 0.72,
                "version": 0.58, "semantic": 0.68}[detector_group]
        latency_s += base * (0.82 + 0.36 * stable_uniform(seed, opp["opportunity_id"], "latency"))
    return {
        **extraction,
        "pages_total": len(current_pages) + len(prior_pages),
        "pages_read": len(read_current) + len(read_prior),
        "current_tables_total": len(current_pages),
        "incomplete_current_tables": incomplete_current_tables,
        "coverage_gate": coverage_gate,
        "input_tokens_simulated": input_tokens,
        "output_tokens_simulated": output_tokens,
        "latency_seconds_simulated_serial": round(latency_s, 6),
    }


def run_systems(cases: list[dict], seed: int) -> dict[str, dict[str, Any]]:
    outputs = {s: {"predictions": [], "project_stats": {}, "guard_stats": {
        "verification_dropped": 0, "duplicates_dropped": 0,
    }} for s in SYSTEMS}
    for case in cases:
        pid = case["project_id"]
        rules = structural_rules(case, seed)
        raw_llm = simulated_llm_raw(case, seed)

        outputs[RULES_ONLY]["predictions"].extend(rules)
        # Rules operate on the supplied structural facts and have no LLM extraction.
        outputs[RULES_ONLY]["project_stats"][pid] = {
            "pages_total": len(case["pages"]), "pages_read": len(case["pages"]),
            "current_tables_total": 10, "incomplete_current_tables": [],
            "coverage_gate": False, "rule_operations": len(STRUCTURAL_FAMILIES),
            "input_tokens_simulated": 0, "output_tokens_simulated": 0,
            "latency_seconds_simulated_serial": 0.0,
            "rows_total": 0, "rows_extracted": 0,
            "numeric_cells_evaluated": 0, "numeric_cells_exact": 0,
            "cells_recovered_by_self_check": 0,
        }

        outputs[LLM_ONLY]["predictions"].extend(raw_llm)
        outputs[LLM_ONLY]["project_stats"][pid] = _operational_stats(
            case, raw_llm, seed, self_check=False, coverage_gate=False)

        verified, n_verify = verify_predictions(raw_llm)
        combined = list(rules) + [dict(p, prediction_id=p["prediction_id"].replace(
            f"{LLM_ONLY}:", f"{HYBRID}:", 1)) for p in verified]
        hybrid, n_dupe = dedupe_predictions(combined)
        outputs[HYBRID]["predictions"].extend(hybrid)
        outputs[HYBRID]["guard_stats"]["verification_dropped"] += n_verify
        outputs[HYBRID]["guard_stats"]["duplicates_dropped"] += n_dupe
        outputs[HYBRID]["project_stats"][pid] = _operational_stats(
            case, hybrid, seed, self_check=True, coverage_gate=True)
        outputs[HYBRID]["project_stats"][pid]["rule_operations"] = len(STRUCTURAL_FAMILIES)
    return outputs
