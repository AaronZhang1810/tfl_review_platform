"""Unit tests for the pure (no-model, no-DB) parts of the AI-review inversion:
the extraction self-check detector (Precision guard #1) and the arithmetic
verify pass (Precision guard #2)."""

import pytest

import ai_review


def test_public_tree_defaults_to_the_fictional_checklist_when_root_config_is_absent():
    config = ai_review.load_config()
    assert isinstance(config["checklist"], list)
    assert config["checklist"]


def _valid_extraction():
    return {
        "analysis_set": "Safety Analysis Set", "header_label": "Table 1",
        "groups": [{"label": "Total", "n": 10}],
        "summary_rows": [{"label": "Participants", "values": {"Total": 10}}],
    }


@pytest.mark.parametrize("bad", [None, [], "clean", {"analysis_set": "x", "header_label": "x", "groups": {}}])
def test_extraction_validator_rejects_malformed_response(bad):
    with pytest.raises(ai_review.AIReviewResponseError):
        ai_review._validate_extraction_response(bad)


def test_extraction_validator_accepts_and_normalizes_valid_response():
    got = ai_review._validate_extraction_response(_valid_extraction())
    assert got["groups"][0]["n"] == 10
    assert got["footnote_markers"] == []
    assert got["pt_terms"] == []


def test_judge_builder_rejects_non_list_and_unknown_item():
    item = {"id": "2.1", "scope": "within_table", "risk": "High"}
    with pytest.raises(ai_review.AIReviewResponseError):
        ai_review._build_judge_findings({}, {"checklist": [item]}, {"2.1": item}, "within_table")
    with pytest.raises(ai_review.AIReviewResponseError):
        ai_review._build_judge_findings(
            [{"checklist_item": "9.9", "message": "x", "operation": "none"}],
            {"checklist": [item]}, {"2.1": item}, "within_table",
        )


def test_self_check_malformed_response_propagates(monkeypatch):
    ex = {"groups": [{"label": "Total", "n": 10}],
          "summary_rows": [{"label": "Participants", "values": {"Total": 11}}]}
    monkeypatch.setattr(ai_review.ai_client, "call_structured", lambda *a, **k: {"wrong": []})
    with pytest.raises(ai_review.AIReviewResponseError):
        ai_review.self_check("Participants 11", ex, {"selfcheck": {"enabled": True}})


def test_load_config_parse_error_propagates(tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(ai_review, "_CFG_PATH", str(bad))
    with pytest.raises(RuntimeError, match="could not be loaded"):
        ai_review.load_config()


# --- Precision guard #1: extraction self-check detector -------------------- #

def test_suspect_cells_flags_pct_inconsistency():
    # 60/100 = 60.0% printed as 55.0% → the extraction is internally inconsistent.
    ex = {"groups": [{"label": "Mono", "n": 100}],
          "summary_rows": [{"label": "Any AE", "values": {"Mono": 60}, "pcts": {"Mono": 55.0}}]}
    sc = ai_review.suspect_cells(ex, tol=0.6)
    assert len(sc) == 1
    assert sc[0]["row_label"] == "Any AE" and sc[0]["group"] == "Mono"


def test_suspect_cells_flags_n_greater_than_N():
    ex = {"groups": [{"label": "Mono", "n": 100}],
          "summary_rows": [{"label": "Bad", "values": {"Mono": 120}, "pcts": {"Mono": 120.0}}]}
    assert any(s["group"] == "Mono" and s["n"] == 120 for s in ai_review.suspect_cells(ex))


def test_suspect_cells_clean_row_not_flagged():
    ex = {"groups": [{"label": "Mono", "n": 100}],
          "summary_rows": [{"label": "Any AE", "values": {"Mono": 98}, "pcts": {"Mono": 98.0}}]}
    assert ai_review.suspect_cells(ex, tol=0.6) == []


def test_suspect_cells_skips_count_row_without_pct():
    # No percent + n<=N → nothing to reconcile (e.g. an event-count row) → not suspect.
    ex = {"groups": [{"label": "Mono", "n": 1000}],
          "summary_rows": [{"label": "Number of events", "values": {"Mono": 500}}]}
    assert ai_review.suspect_cells(ex) == []


# --- Precision guard #2: verify pass (re-check the AI's own arithmetic) ----- #

VP = {"verify_pass": {"enabled": True, "count_tolerance": 0}}


def test_verify_keeps_finding_whose_math_truly_contradicts():
    # 1240 + 6 = 1246, but the finding claims the printed total is 1250 → real gap.
    f = {"check_id": "AIX-8", "operation": "sum_equals",
         "cited_numbers": [1240, 6], "observed": 1250, "message": "x"}
    kept, dropped = ai_review.verify_findings([f], VP)
    assert len(kept) == 1 and dropped == 0


def test_verify_drops_self_consistent_finding():
    # 1240 + 6 = 1246 = observed → the AI's own arithmetic reconciles → LLM slip → drop.
    f = {"check_id": "AIX-8", "operation": "sum_equals",
         "cited_numbers": [1240, 6], "observed": 1246, "message": "x"}
    kept, dropped = ai_review.verify_findings([f], VP)
    assert kept == [] and dropped == 1


def test_verify_keeps_qualitative_and_underdetermined():
    # 'none' is qualitative (no arithmetic); sum_equals with no observed can't be
    # verified — neither may be silently suppressed.
    q = {"operation": "none", "cited_numbers": [], "observed": None, "message": "footnote wrong"}
    u = {"operation": "sum_equals", "cited_numbers": [1, 2], "observed": None, "message": "y"}
    kept, dropped = ai_review.verify_findings([q, u], VP)
    assert dropped == 0 and len(kept) == 2


def test_verify_handles_less_equal_and_version_ops():
    le = {"operation": "less_equal", "cited_numbers": [120, 100], "observed": None, "message": "n>N"}
    dec = {"operation": "decreased", "cited_numbers": [100, 90], "observed": None, "message": "N fell"}
    notdec = {"operation": "decreased", "cited_numbers": [100, 110], "observed": None, "message": "wrong"}
    kept, dropped = ai_review.verify_findings([le, dec, notdec], VP)
    msgs = {f["message"] for f in kept}
    assert "n>N" in msgs and "N fell" in msgs
    assert "wrong" not in msgs and dropped == 1


def test_verify_pass_disabled_is_passthrough():
    # Self-consistent finding (1+2==3) survives only because the pass is off.
    f = {"operation": "sum_equals", "cited_numbers": [1, 2], "observed": 3, "message": "z"}
    kept, dropped = ai_review.verify_findings([f], {"verify_pass": {"enabled": False}})
    assert len(kept) == 1 and dropped == 0
