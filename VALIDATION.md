# Verification and Simulated-Evaluation Plan

## Status

This document describes engineering verification for a fictional portfolio demonstration. It is not a computer-system-validation package, a GxP determination, or evidence that the application is fit for a regulated use.

## Intended demonstration

The application assists a reviewer by indexing TLF PDFs, running deterministic structural checks, extracting table facts, generating AI review candidates, and recording human actions. It does not generate a regulatory conclusion and must not replace source-data verification or qualified review.

## What is deterministic and what is not

| Layer | Behavior | Verification approach |
|---|---|---|
| PDF indexing | Bookmark/caption parsing and page ranges | Unit tests with generated PDFs |
| Structural checks | Blank pages, missing outputs, numbering gaps | Exact expected findings |
| Structured extraction | Model converts page text to a schema | Simulated labeled fields and error analysis |
| AI judgment | Model proposes checklist-based candidates | Precision/recall by check family and repeated-run analysis |
| Numeric verification | Python recomputes arithmetic cited by a candidate | Unit and property-oriented cases |
| Human workflow | Post, reject, reopen, comment, import/export | API and round-trip tests |

The AI performs both extraction and parts of the judgment. Deterministic verification can reject an arithmetically unsupported candidate, but it cannot prove that the model read every relevant row or applied a qualitative rule correctly.

## Simulated evaluation set

`run_demo.py` and `evaluation/` generate fictional documents with seeded, known conditions. The corpus should include:

- clean tables;
- subgroup and row-total mismatches;
- incorrect percentages and `n > N` cases;
- blank pages and output-numbering gaps;
- current/prior count increases and decreases;
- cross-output population mismatches;
- renamed or moved fictional terms;
- partial extraction, malformed model output, and unavailable-model cases.

Each labeled issue records its document, output, page, check family, severity, expected evidence, and generation seed. A case may be negative for one check and positive for another. Train or prompt-development examples must be separated from the final evaluation partition.

## Metrics

Report at least:

- candidate precision, recall, and F1 by check family;
- document-level false-positive rate;
- extraction field accuracy for labels, counts, denominators, and percentages;
- page/output localization accuracy;
- deterministic-verification rejection rate;
- coverage: pages attempted, pages successfully read, and outputs skipped;
- latency and model token usage, clearly labeled as environment-specific;
- repeated-run disagreement for non-deterministic model paths.

Use exact binomial confidence intervals or a clearly documented bootstrap for rates. Do not present results from the simulated corpus as clinical performance.

## Test and CI gates

The checked-in CI workflow performs:

1. dependency installation in Python 3.12;
2. installation of the fictional public checklist;
3. the public-release content audit;
4. ruff static checks;
5. the pytest suite; and
6. a dependency vulnerability audit.

A release candidate additionally requires a clean fresh-environment run, successful Docker health check, reproducible simulated-evaluation output, and manual inspection of the generated documents.

For each checked benchmark run, `benchmark_config.json` records the source-tree SHA-256 and exact development-lock SHA-256, while `artifact_hashes.txt` pins the deterministic artifacts. Machine-dependent timing stays in `runtime_environment.json` and is not part of the reproducibility hash contract.

## Known limitations

- Dense, rotated, scanned, or image-only tables can be missed or misread.
- Listings and figures are not comprehensively analyzed.
- A “no candidate” result is not evidence that an output is correct.
- Model behavior can change with model, prompt, provider, and service configuration.
- Local SQLite files and uploads are not encrypted by the application.
- The local edition has no login, roles, tenant isolation, or network deployment controls.
- The simulated corpus cannot reproduce the diversity or prevalence of production errors.

## Change control for the demonstration

Changes to prompts, schemas, model selection, check configuration, extraction logic, or evaluation generation require a new source digest and a full simulated regression run. Keep generated metrics with the seed, dependency inventory, model identifier, prompt/configuration hash, and code revision used to produce them.
