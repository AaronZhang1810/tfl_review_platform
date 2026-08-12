"""One-to-one finding matching, operational metrics, and clustered bootstrap."""

from __future__ import annotations

import math
import random
from typing import Any, Iterable

from .catalog import FAMILY_BY_ID, FAMILIES
from .systems import HYBRID, LLM_ONLY, RULES_ONLY, SYSTEMS


def finding_key(row: dict) -> tuple:
    loc = row["locator"]
    return (
        row["project_id"], row["family"], loc.get("output_label"),
        loc.get("row"), loc.get("column"), loc.get("comparison_output"),
    )


def one_to_one_match(truth: list[dict], predictions: list[dict]) -> dict:
    """Exact structured matching; message wording is deliberately ignored."""
    truth_by_key: dict[tuple, list[dict]] = {}
    for item in truth:
        truth_by_key.setdefault(finding_key(item), []).append(item)
    used: set[str] = set()
    matched = []
    false_positives = []
    for pred in predictions:
        candidates = truth_by_key.get(finding_key(pred), [])
        target = next((t for t in candidates if t["truth_id"] not in used), None)
        if target is None:
            false_positives.append(pred)
        else:
            used.add(target["truth_id"])
            matched.append({"truth_id": target["truth_id"],
                            "prediction_id": pred["prediction_id"],
                            "project_id": target["project_id"],
                            "family": target["family"]})
    missed = [t for t in truth if t["truth_id"] not in used]
    return {"matched": matched, "false_positives": false_positives, "missed": missed}


def _safe_ratio(num: float, den: float) -> float | None:
    return num / den if den else None


def _f1(precision: float | None, recall: float | None) -> float | None:
    # If truth positives exist but a system emits no prediction, precision is undefined while recall is zero; its F1 is conventionally zero, not missing.
    if precision is None or recall is None:
        return None if precision is None and recall is None else 0.0
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def _project_record(case: dict, truth: list[dict], predictions: list[dict],
                    stats: dict) -> tuple[dict, dict]:
    match = one_to_one_match(truth, predictions)
    tp = len(match["matched"])
    fp = len(match["false_positives"])
    fn = len(match["missed"])
    high_truth = {t["truth_id"] for t in truth if t["risk"] == "High"}
    matched_truth = {m["truth_id"] for m in match["matched"]}

    tables = {f"Table {i}" for i in range(1, 11)}
    truth_tables = {t["locator"]["output_label"] for t in truth}
    pred_tables = {p["locator"]["output_label"] for p in predictions}
    clean_tables = tables - truth_tables
    predicted_clean = tables - pred_tables
    incomplete = set(stats.get("incomplete_current_tables", []))
    eligible_autoapproval = predicted_clean - incomplete if stats.get("coverage_gate") else predicted_clean

    family_counts = {}
    matched_by_family = {}
    fp_by_family = {}
    missed_by_family = {}
    for m in match["matched"]:
        matched_by_family[m["family"]] = matched_by_family.get(m["family"], 0) + 1
    for p in match["false_positives"]:
        fp_by_family[p["family"]] = fp_by_family.get(p["family"], 0) + 1
    for t in match["missed"]:
        missed_by_family[t["family"]] = missed_by_family.get(t["family"], 0) + 1
    for family in FAMILIES:
        family_counts[family.id] = {
            "tp": matched_by_family.get(family.id, 0),
            "fp": fp_by_family.get(family.id, 0),
            "fn": missed_by_family.get(family.id, 0),
        }

    record = {
        "project_id": case["project_id"],
        "tp": tp, "fp": fp, "fn": fn,
        "high_tp": len(high_truth & matched_truth),
        "high_fn": len(high_truth - matched_truth),
        "current_tables": len(tables),
        "clean_tables": len(clean_tables),
        "true_negative_tables": len(clean_tables - pred_tables),
        "false_positive_tables": len(clean_tables & pred_tables),
        "issue_bearing_tables": len(truth_tables),
        "issue_tables_predicted_clean": len(truth_tables & predicted_clean),
        "predicted_clean_tables": len(predicted_clean),
        "autoapproved_tables": len(eligible_autoapproval),
        "unsafe_autoapproved_tables": len(truth_tables & eligible_autoapproval),
        "pages_total": stats.get("pages_total", 0),
        "pages_read": stats.get("pages_read", 0),
        "rows_total": stats.get("rows_total", 0),
        "rows_extracted": stats.get("rows_extracted", 0),
        "numeric_cells_evaluated": stats.get("numeric_cells_evaluated", 0),
        "numeric_cells_exact": stats.get("numeric_cells_exact", 0),
        "cells_recovered_by_self_check": stats.get("cells_recovered_by_self_check", 0),
        "input_tokens_simulated": stats.get("input_tokens_simulated", 0),
        "output_tokens_simulated": stats.get("output_tokens_simulated", 0),
        "latency_seconds_simulated_serial": stats.get("latency_seconds_simulated_serial", 0.0),
        "family_counts": family_counts,
    }
    return record, match


def build_project_records(cases: list[dict], truth: list[dict],
                          outputs: dict[str, dict]) -> tuple[dict[str, list[dict]], dict[str, dict]]:
    truth_by_project: dict[str, list] = {}
    for item in truth:
        truth_by_project.setdefault(item["project_id"], []).append(item)
    case_by_project = {c["project_id"]: c for c in cases}
    records: dict[str, list[dict]] = {}
    matches: dict[str, dict] = {}
    for system in SYSTEMS:
        pred_by_project: dict[str, list] = {}
        for pred in outputs[system]["predictions"]:
            pred_by_project.setdefault(pred["project_id"], []).append(pred)
        rows = []
        all_matched = []
        all_fp = []
        all_missed = []
        for pid in sorted(case_by_project):
            row, match = _project_record(
                case_by_project[pid], truth_by_project.get(pid, []),
                pred_by_project.get(pid, []), outputs[system]["project_stats"][pid])
            rows.append(row)
            all_matched.extend(match["matched"])
            all_fp.extend(match["false_positives"])
            all_missed.extend(match["missed"])
        records[system] = rows
        matches[system] = {"matched": all_matched, "false_positives": all_fp,
                           "missed": all_missed}
    return records, matches


def _sum(records: Iterable[dict], key: str) -> float:
    return sum(float(r.get(key, 0) or 0) for r in records)


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    pos = (len(values) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return values[lo]
    return values[lo] * (hi - pos) + values[hi] * (pos - lo)


def aggregate_metrics(records: list[dict], *, input_price: float | None = None,
                      output_price: float | None = None) -> dict[str, Any]:
    tp, fp, fn = _sum(records, "tp"), _sum(records, "fp"), _sum(records, "fn")
    precision = _safe_ratio(tp, tp + fp)
    recall = _safe_ratio(tp, tp + fn)
    family_metrics = {}
    family_f1 = []
    for family in FAMILIES:
        ftp = sum(r["family_counts"][family.id]["tp"] for r in records)
        ffp = sum(r["family_counts"][family.id]["fp"] for r in records)
        ffn = sum(r["family_counts"][family.id]["fn"] for r in records)
        p = _safe_ratio(ftp, ftp + ffp)
        rec = _safe_ratio(ftp, ftp + ffn)
        f = _f1(p, rec)
        if f is not None:
            family_f1.append(f)
        family_metrics[family.id] = {
            "title": family.title, "risk": family.risk,
            "tp": ftp, "fp": ffp, "fn": ffn,
            "precision": p, "recall": rec, "f1": f,
        }
    pages_total = _sum(records, "pages_total")
    tokens_in = _sum(records, "input_tokens_simulated")
    tokens_out = _sum(records, "output_tokens_simulated")
    cost = None
    if input_price is not None and output_price is not None:
        cost = tokens_in / 1_000_000 * input_price + tokens_out / 1_000_000 * output_price
    latencies = [float(r["latency_seconds_simulated_serial"]) for r in records]
    numeric_eval = _sum(records, "numeric_cells_evaluated")
    result = {
        "counts": {"tp": int(tp), "fp": int(fp), "fn": int(fn),
                   "predictions": int(tp + fp), "truth": int(tp + fn)},
        "precision": precision,
        "recall": recall,
        "f1": _f1(precision, recall),
        "high_risk_recall": _safe_ratio(_sum(records, "high_tp"),
                                         _sum(records, "high_tp") + _sum(records, "high_fn")),
        "macro_f1": sum(family_f1) / len(family_f1) if family_f1 else None,
        "false_positive_findings_per_100_tables": 100 * _safe_ratio(fp, _sum(records, "current_tables")),
        "clean_table_specificity": _safe_ratio(_sum(records, "true_negative_tables"),
                                                 _sum(records, "clean_tables")),
        "issue_table_miss_rate": _safe_ratio(_sum(records, "issue_tables_predicted_clean"),
                                               _sum(records, "issue_bearing_tables")),
        "unsafe_autoapproval_rate": _safe_ratio(_sum(records, "unsafe_autoapproved_tables"),
                                                  _sum(records, "autoapproved_tables")),
        "coverage_rate": _safe_ratio(_sum(records, "pages_read"), pages_total),
        "extraction_row_recall": _safe_ratio(_sum(records, "rows_extracted"),
                                               _sum(records, "rows_total")),
        "numeric_cell_exact_accuracy": _safe_ratio(_sum(records, "numeric_cells_exact"),
                                                     numeric_eval),
        "cells_recovered_by_self_check": int(_sum(records, "cells_recovered_by_self_check")),
        "simulated_usage": {
            "input_tokens": int(tokens_in), "output_tokens": int(tokens_out),
            "input_tokens_per_1000_pages": 1000 * _safe_ratio(tokens_in, pages_total),
            "output_tokens_per_1000_pages": 1000 * _safe_ratio(tokens_out, pages_total),
            "cost_usd": cost,
            "cost_note": ("Modeled from caller-supplied prices; no API call occurred."
                          if cost is not None else "N/A — no token prices supplied; no API call occurred."),
        },
        "simulated_latency": {
            "median_serial_seconds_per_project": _quantile(latencies, 0.5),
            "p95_serial_seconds_per_project": _quantile(latencies, 0.95),
            "total_serial_seconds": sum(latencies),
            "note": "Deterministic latency proxy, not measured model latency.",
        },
        "families": family_metrics,
    }
    return result


BOOTSTRAP_KEYS = (
    "precision", "recall", "f1", "high_risk_recall", "macro_f1",
    "false_positive_findings_per_100_tables", "clean_table_specificity",
    "issue_table_miss_rate", "unsafe_autoapproval_rate", "coverage_rate",
    "extraction_row_recall", "numeric_cell_exact_accuracy",
)


def bootstrap_metrics(records: dict[str, list[dict]], *, iterations: int = 2000,
                      seed: int = 20260808) -> tuple[dict, dict]:
    """Paired percentile bootstrap, resampling whole projects as clusters."""
    n = len(next(iter(records.values())))
    rng = random.Random(seed)
    samples = [[rng.randrange(n) for _ in range(n)] for _ in range(iterations)]
    draws: dict[str, dict[str, list[float]]] = {
        system: {key: [] for key in BOOTSTRAP_KEYS} for system in SYSTEMS}
    for indices in samples:
        for system in SYSTEMS:
            metric = aggregate_metrics([records[system][i] for i in indices])
            for key in BOOTSTRAP_KEYS:
                value = metric.get(key)
                if value is not None:
                    draws[system][key].append(float(value))

    cis = {system: {} for system in SYSTEMS}
    for system in SYSTEMS:
        for key, values in draws[system].items():
            cis[system][key] = {
                "low": _quantile(values, 0.025), "high": _quantile(values, 0.975),
                "iterations": iterations,
            }

    comparisons = {}
    for base in (RULES_ONLY, LLM_ONLY):
        name = f"{HYBRID}_minus_{base}"
        comparisons[name] = {}
        for key in BOOTSTRAP_KEYS:
            paired = [h - b for h, b in zip(draws[HYBRID][key], draws[base][key])]
            comparisons[name][key] = {
                "low": _quantile(paired, 0.025), "high": _quantile(paired, 0.975),
                "iterations": iterations,
            }
    return cis, comparisons


def score_all(cases: list[dict], truth: list[dict], outputs: dict[str, dict], *,
              bootstrap_iterations: int = 2000, seed: int = 20260808,
              input_price: float | None = None,
              output_price: float | None = None) -> tuple[dict, dict, dict]:
    records, matches = build_project_records(cases, truth, outputs)
    metrics = {system: aggregate_metrics(records[system], input_price=input_price,
                                         output_price=output_price)
               for system in SYSTEMS}
    cis, comparison_cis = bootstrap_metrics(records, iterations=bootstrap_iterations,
                                            seed=seed + 917)
    for system in SYSTEMS:
        metrics[system]["ci95"] = cis[system]
        metrics[system]["guard_stats"] = outputs[system]["guard_stats"]

    comparisons = {}
    for base in (RULES_ONLY, LLM_ONLY):
        key = f"{HYBRID}_minus_{base}"
        comparisons[key] = {}
        for metric_key in BOOTSTRAP_KEYS:
            hv = metrics[HYBRID].get(metric_key)
            bv = metrics[base].get(metric_key)
            comparisons[key][metric_key] = {
                "estimate": (hv - bv) if hv is not None and bv is not None else None,
                "ci95": comparison_cis[key][metric_key],
            }
    return metrics, comparisons, {"records": records, "matches": matches}
