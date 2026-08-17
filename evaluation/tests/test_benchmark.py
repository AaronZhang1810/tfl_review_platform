from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from evaluation.catalog import DISCLAIMER, FAMILY_BY_ID, FAMILIES, STRUCTURAL_FAMILIES
from evaluation.generate import (evidence_is_violation, generate_dataset,
                                 opportunity_difficulty, validate_dataset)
from evaluation.run_benchmark import _canonicalize_floats, reproducibility_record, run
from evaluation.scoring import one_to_one_match, score_all
from evaluation.systems import (HYBRID, LLM_ONLY, RULES_ONLY, dedupe_predictions,
                                run_systems, verify_predictions)


class GenerationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases, cls.truth = generate_dataset()

    def test_published_dataset_card_invariants(self):
        validate_dataset(self.cases, self.truth, n_projects=50,
                         positives_per_family=10)
        self.assertEqual(len(FAMILIES), 17)
        self.assertEqual(len(self.cases), 50)
        self.assertEqual(sum(len(c["pages"]) for c in self.cases), 1000)
        self.assertEqual(len(self.truth), 170)
        self.assertTrue(all(len(c["opportunities"]) == 17 for c in self.cases))

    def test_exactly_ten_issues_per_family(self):
        counts = {f.id: 0 for f in FAMILIES}
        for item in self.truth:
            counts[item["family"]] += 1
        self.assertEqual(set(counts.values()), {10})

    def test_truth_is_separate_and_observable_evidence_reconstructs_it(self):
        self.assertTrue(all("is_issue" not in o for c in self.cases
                            for o in c["opportunities"]))
        inferred = {o["opportunity_id"] for c in self.cases for o in c["opportunities"]
                    if evidence_is_violation(o)}
        labeled = {t["opportunity_id"] for t in self.truth}
        self.assertEqual(inferred, labeled)

    def test_opportunity_records_are_normalized(self):
        expected = {"opportunity_id", "project_id", "family", "locator", "evidence"}
        locator_fields = {
            "output_label", "page", "row", "column", "comparison_output",
        }
        for case in self.cases:
            for opportunity in case["opportunities"]:
                self.assertEqual(set(opportunity), expected)
                self.assertEqual(set(opportunity["locator"]), locator_fields)
                family = FAMILY_BY_ID[opportunity["family"]]
                self.assertEqual(
                    opportunity["locator"]["comparison_output"] is not None,
                    family.scope == "cross_output",
                )
                self.assertIn(opportunity_difficulty(opportunity),
                              {"easy", "medium", "hard"})

    def test_truth_records_derive_risk_from_taxonomy(self):
        expected = {
            "truth_id", "opportunity_id", "project_id", "family", "locator",
            "evidence",
        }
        self.assertTrue(all(set(item) == expected for item in self.truth))
        self.assertEqual(
            sum(FAMILY_BY_ID[item["family"]].risk == "High" for item in self.truth),
            120,
        )

    def test_concrete_within_table_opportunity_uses_clean_schema(self):
        opportunity = next(
            item
            for case in self.cases
            for item in case["opportunities"]
            if item["opportunity_id"] == "SYN-P005:AIW-2.1"
        )
        self.assertEqual(
            opportunity,
            {
                "opportunity_id": "SYN-P005:AIW-2.1",
                "project_id": "SYN-P005",
                "family": "AIW-2.1",
                "locator": {
                    "output_label": "Table 4",
                    "page": 4,
                    "row": "SYNTHETIC ROW 4",
                    "column": "Drug X",
                    "comparison_output": None,
                },
                "evidence": {"addends": [45, 49], "printed_total": 91},
            },
        )

    def test_generation_is_reproducible(self):
        cases2, truth2 = generate_dataset()
        self.assertEqual(self.cases, cases2)
        self.assertEqual(self.truth, truth2)


class SystemAndScoringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases, cls.truth = generate_dataset(n_projects=10,
                                                positives_per_family=2)
        cls.outputs = run_systems(cls.cases, 20260808)

    def test_rules_only_contains_current_structural_families_only(self):
        families = {p["family"] for p in self.outputs[RULES_ONLY]["predictions"]}
        self.assertTrue(families <= STRUCTURAL_FAMILIES)
        self.assertEqual(len(self.outputs[RULES_ONLY]["predictions"]), 6)

    def test_one_to_one_matching_penalizes_duplicate(self):
        truth = [self.truth[0]]
        opp = next(o for c in self.cases for o in c["opportunities"]
                   if o["opportunity_id"] == truth[0]["opportunity_id"])
        pred = {
            "prediction_id": "p1", "project_id": opp["project_id"],
            "family": opp["family"], "locator": opp["locator"],
        }
        duplicate = dict(pred, prediction_id="p2")
        result = one_to_one_match(truth, [pred, duplicate])
        self.assertEqual(len(result["matched"]), 1)
        self.assertEqual(len(result["false_positives"]), 1)

    def test_numeric_verification_and_deduplication(self):
        numeric_fp = {
            "operation": "equals", "cited_numbers": [10, 10], "observed": None,
            "family": "AIX-3", "locator": {"page": 1}, "numbers": [10, 10],
            "message": "same claim", "prediction_id": "a",
        }
        qualitative = {
            "operation": "none", "cited_numbers": [], "observed": None,
            "family": "AIX-5", "locator": {"page": 1}, "numbers": [],
            "message": "qualitative claim", "prediction_id": "b",
        }
        kept, dropped = verify_predictions([numeric_fp, qualitative])
        self.assertEqual(dropped, 1)
        self.assertEqual(kept, [qualitative])
        deduped, n = dedupe_predictions([qualitative, dict(qualitative, prediction_id="c")])
        self.assertEqual(len(deduped), 1)
        self.assertEqual(n, 1)

    def test_metrics_and_cluster_bootstrap_are_present(self):
        metrics, comparisons, _ = score_all(
            self.cases, self.truth, self.outputs, bootstrap_iterations=40)
        self.assertEqual(metrics[RULES_ONLY]["counts"]["truth"], 34)
        self.assertGreater(metrics[HYBRID]["precision"], metrics[LLM_ONLY]["precision"])
        self.assertIn("high_risk_recall", metrics[HYBRID]["ci95"])
        self.assertEqual(metrics[HYBRID]["ci95"]["high_risk_recall"]["iterations"], 40)
        self.assertIn(f"{HYBRID}_minus_{LLM_ONLY}", comparisons)
        self.assertIsNone(metrics[LLM_ONLY]["simulated_usage"]["cost_usd"])


class ArtifactTests(unittest.TestCase):
    def test_artifact_floats_are_canonicalized(self):
        value = {
            "metric": 0.123456789012345,
            "negative_zero": -1e-16,
            "nested": [1.0000000000004],
        }
        self.assertEqual(
            _canonicalize_floats(value),
            {"metric": 0.123456789012, "negative_zero": 0.0, "nested": [1.0]},
        )

    def test_artifacts_are_reproducible_and_disclaimed(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            ra = run(Path(a), n_projects=10, positives_per_family=2,
                     bootstrap_iterations=30)
            rb = run(Path(b), n_projects=10, positives_per_family=2,
                     bootstrap_iterations=30)
            self.assertEqual((Path(a) / "artifact_hashes.txt").read_text(),
                             (Path(b) / "artifact_hashes.txt").read_text())
            report = (Path(a) / "report.html").read_text()
            self.assertIn(DISCLAIMER, report)
            self.assertIn("seeded behavioral simulation", report)
            markdown = (Path(a) / "REPORT.md").read_text()
            self.assertIn(DISCLAIMER, markdown)
            self.assertIn("Executable source SHA-256", markdown)
            config = json.loads((Path(a) / "benchmark_config.json").read_text())
            self.assertEqual(config["families"], 17)
            self.assertEqual(config["bootstrap_iterations"], 30)
            self.assertEqual(config["reproducibility"], reproducibility_record())
            self.assertRegex(
                config["reproducibility"]["source_tree_sha256"],
                r"^[0-9a-f]{64}$",
            )
            self.assertIn("rules_only_detection_seconds_measured", ra["runtime"])
            self.assertIn("rules_only_detection_seconds_measured", rb["runtime"])

    def test_checked_artifacts_match_current_source_and_reference_configuration(self):
        checked = Path(__file__).resolve().parents[1] / "artifacts"
        config = json.loads((checked / "benchmark_config.json").read_text())
        self.assertEqual(config["reproducibility"], reproducibility_record())
        with tempfile.TemporaryDirectory() as temporary:
            generated = Path(temporary)
            run(generated)
            hashes = (checked / "artifact_hashes.txt").read_text()
            self.assertEqual(hashes, (generated / "artifact_hashes.txt").read_text())
            for line in hashes.splitlines():
                if not line or line.startswith("#"):
                    continue
                _, name = line.split("  ", 1)
                self.assertEqual((checked / name).read_bytes(), (generated / name).read_bytes())
            public_report = checked.parent / "REPORT.md"
            self.assertEqual(public_report.read_bytes(), (generated / "REPORT.md").read_bytes())


if __name__ == "__main__":
    unittest.main()
