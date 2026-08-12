"""Versioned benchmark taxonomy and constants."""

from __future__ import annotations

from dataclasses import asdict, dataclass

BENCHMARK_VERSION = "synthetic-tlf-benchmark-v1"
SIMULATOR_VERSION = "simulated-llm-v1"
BENCHMARK_SEED = 20260808
DISCLAIMER = (
    "SYNTHETIC ENGINEERING BENCHMARK — all studies, tables, counts, and model "
    "behaviors are simulated. Results do not measure performance on real TLFs, "
    "do not constitute clinical validation, and do not establish regulatory fitness."
)


@dataclass(frozen=True)
class Family:
    id: str
    title: str
    risk: str
    scope: str
    operation: str
    detector_group: str


# Exactly 17 executable finding families. Cross-document checklist item 9 is intentionally excluded because the production code gates it out.
FAMILIES: tuple[Family, ...] = (
    Family("FMT-010", "Blank or empty page", "Low", "structural", "blank", "structural"),
    Family("XOUT-020", "Output missing versus prior edition", "Low", "structural", "missing_current", "structural"),
    Family("XOUT-001", "Gap in output numbering", "High", "structural", "number_gap", "structural"),
    Family("AIW-2.1", "Subgroups do not sum to heading N", "High", "within_table", "sum_equals", "arithmetic"),
    Family("AIW-2.2", "Component columns do not sum to row total", "High", "within_table", "sum_equals", "arithmetic"),
    Family("AIW-2.3", "Within-table numeric integrity", "High", "within_table", "less_equal", "arithmetic"),
    Family("AIX-3", "Pooled-indication N mismatch", "High", "cross_output", "equals", "cross_output"),
    Family("AIX-4", "Table 1 versus by-study N mismatch", "High", "cross_output", "equals", "cross_output"),
    Family("AIX-5", "Footnote study-list mismatch", "Low", "cross_output", "set_equals", "semantic"),
    Family("AIV-6.1", "New study versus prior edition", "Low", "version", "new_present", "version"),
    Family("AIV-6.2", "N decreased versus prior edition", "High", "version", "decreased", "version"),
    Family("AIV-6.3", "N increased versus prior edition", "Low", "version", "increased", "version"),
    Family("AIX-7.1", "AE overview does not match SOC/PT", "High", "cross_output", "equals", "cross_output"),
    Family("AIW-7.2", "Forbidden zero in Any AE row", "High", "within_table", "zero_forbidden", "arithmetic"),
    Family("AIV-7.3", "AE-overview change versus prior", "High", "version", "decreased", "version"),
    Family("AIV-7.4", "Preferred term renamed or moved", "High", "version", "term_changed", "semantic"),
    Family("AIX-8", "By-study output mismatch", "High", "cross_output", "equals", "cross_output"),
)

FAMILY_BY_ID = {f.id: f for f in FAMILIES}
STRUCTURAL_FAMILIES = frozenset({"FMT-010", "XOUT-020", "XOUT-001"})
NUMERIC_OPERATIONS = frozenset({"sum_equals", "equals", "less_equal", "decreased", "increased"})


def taxonomy_json() -> list[dict]:
    return [asdict(f) for f in FAMILIES]
