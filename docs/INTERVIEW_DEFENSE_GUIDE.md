# TLF Review Platform: Implementation and Interview-Defense Guide

This document explains the application at code level: the normal runtime workflow, the role of each important module and function, where an external model is involved, where Python remains authoritative, how state is stored, how the synthetic demo differs from the benchmark, and which limitations must be stated honestly.

The most important distinction is:

> The normal source application can call a configured Claude model through the Anthropic SDK. The one-click demo and checked evaluation do not call Claude. The demo pre-seeds fictional results; the evaluation uses a seeded behavioral simulator.

## 1. The system in one minute

The platform is a local, single-user FastAPI application. A reviewer uploads a current TLF PDF and may add a prior edition and reference documents. Python validates and indexes the files, runs deterministic structural checks, and stores a table-level project index in SQLite. When model-backed review is enabled, the application uses one structured model stage to transcribe table facts and other structured model stages to propose within-table, version-comparison, and cross-output findings. Python then verifies cited arithmetic, removes duplicates, checks review coverage, and decides whether the run is complete. A human remains responsible for accepting, rejecting, reopening, commenting on, and exporting findings.

```text
Browser SPA / PDF.js
        |
        v
FastAPI routes in main.py
        |
        +--> upload validation and PDF indexing ----------- deterministic
        +--> structural checks ---------------------------- deterministic
        +--> review orchestration in runner.py
                 |
                 +--> PDF text extraction ---------------- deterministic parser
                 +--> structured table transcription ----- model-backed
                 +--> suspect-cell reread ---------------- model-backed, narrowly triggered
                 +--> within/version judgment ------------ model-backed
                 +--> cross-output judgment --------------- model-backed
                 +--> arithmetic verification/dedup ------- deterministic
                 +--> completeness/auto-approval policy --- deterministic
        |
        v
SQLite + uploaded files
        |
        v
Human adjudication, comments, annotations, audit, export
```

## 2. Three modes that must not be confused

| Mode | Entry point | External model call? | Purpose |
|---|---|---:|---|
| Normal source application | `python -m uvicorn main:app --host 127.0.0.1 --port 8000` | Optional; yes when a key is configured and the reviewer starts review/chat | Exercise the implemented application on locally uploaded documents |
| One-click synthetic demo | `./run_synthetic_demo.sh` -> `demo/run_demo.py` | No; forcibly disabled | Show the interface and human workflow with generated PDFs and pre-seeded findings |
| Synthetic evaluation | `python -m evaluation.run_benchmark` | No; uses a deterministic simulator | Exercise matching, metrics, guard ablations, bootstrap uncertainty, and reproducible reporting |

Do not describe the demo screenshots or evaluation metrics as measured Claude performance. Likewise, the normal runtime's ability to call Claude does not make the synthetic benchmark a Claude evaluation.

## 3. Normal application workflow

### 3.1 Startup and request boundary

`main.py` constructs the FastAPI application and mounts `static/` after the API routes so `/api/*` takes precedence.

Important functions and controls:

- `main._lifespan()` creates the upload directory, calls `db.init()`, and calls `db.recover_stale_runs()` so a server restart does not leave an AI run displayed as permanently running.
- `TrustedHostMiddleware` restricts accepted hostnames to loopback-oriented `TLF_TRUSTED_HOSTS` values by default.
- `main._same_origin()` and `main._log_requests()` reject cross-origin state-changing browser requests, reject oversized declared requests, add security headers, disable API caching, and log API latency.
- `_read_upload_limited()` and `_save_upload_limited()` enforce byte limits even when `Content-Length` is missing or inaccurate.
- `safe_filename()` removes path components and unsafe characters; `_unique_filename()` prevents two uploaded files from overwriting one another.

AI role: none.

Design defense: validation occurs at the HTTP and parser boundaries because model prompts are not a security boundary. The prototype is still loopback-only because it has no login, authorization, rate limiting, tenant isolation, or application-level encryption.

### 3.2 Project creation and document ingestion

The frontend submits the create-project form to `main.create_project()`.

That route:

1. Requires at least one delivery PDF.
2. Enforces project file-count, per-file, aggregate-upload, and extension limits.
3. Creates a `project` row and a project-specific upload directory.
4. Streams each upload to disk.
5. Calls `project_io.validate_document_file()` to validate supported PDF or Office containers.
6. Calls `runner.detect_edition()` for delivery/prior PDFs.
7. Inserts `document` rows.
8. Calls `indexer.index_delivery()` for every current or prior delivery PDF.
9. Inserts one `output` row per indexed table, listing, or figure.
10. Stores optional TOC, SAP, protocol, and SPP files.
11. Calls `runner.run_structural_checks()` immediately.

If `main_index` is supplied, that PDF becomes role `delivery` and the others become `prior`. Otherwise, `_pick_current_prior()` uses detected edition information to choose the current and comparison editions.

If any critical upload/indexing step fails, the newly created database row and partial upload directory are removed.

AI role: none.

Important limitation: the uploaded TOC workbook is stored, but it is not the current indexing source. SPP is stored but not used by review logic. SAP/protocol text is currently used by chat, not by the TLF judges.

### 3.3 PDF validation, indexing, and clipping

`pdftools.py` owns low-level PDF operations:

- `validate_pdf()` rejects non-PDF headers, encrypted PDFs, empty PDFs, and excessive page counts.
- `page_top_texts()` reads only top-of-page text, preferably through C-backed `pypdfium2`, for fast caption detection.
- `page_texts()` uses `pdfplumber` to extract complete page text for review.
- `clip()` creates a table-level PDF containing an indexed output's page range.
- `file_hash()` hashes raw PDF bytes and caches by path, size, and modification time.
- `content_hash()` hashes extracted text for persisted extraction-cache invalidation.
- `page_char_counts()` supports best-effort blank-page detection. If the underlying parser fails, it returns no counts rather than proving that every page was checked.

`indexer.py` turns a delivery into output records:

- `_parse_label()` recognizes Table, Listing, or Figure labels and separated numbers.
- `_collect_outputs()` ignores section-container bookmarks but retains actual output bookmarks.
- `from_bookmarks()` derives output page ranges from leaf bookmark destinations.
- `from_header_scan()` is the fallback when bookmarks are unusable.
- `_reconcile_with_captions()` treats printed page captions as more authoritative than bookmarks, correcting labels and splitting bookmark ranges that swallowed multiple outputs.
- `index_delivery()` chooses bookmark indexing plus caption reconciliation, or caption scanning alone.

AI role: none.

Design defense: bookmarks are fast and encode hierarchy, but can be wrong or incomplete. Printed captions provide a second source. The system retains a bookmark-derived output if no caption can be parsed rather than silently deleting it.

### 3.4 Deterministic structural checks

`checks.py` implements the current non-model finding generators:

- `blank_page_findings()` -> `FMT-010`
- `missing_output_findings()` -> `XOUT-020`
- `toc_gap_findings()` -> `XOUT-001`

`runner.run_structural_checks()` collects the entire replacement set and publishes it under finding phase `structural` using `_replace_phase_findings()`. That replacement is transactional: if recomputation fails, the previous complete structural snapshot remains.

The checks run immediately after project creation and again at the end of a full review run.

AI role: none.

Design defense: obvious document-structure facts should not be delegated to a probabilistic model. Deterministic checks are cheap, repeatable, and available without an API key.

### 3.5 Starting a full model-backed review

The browser's `startRun()` calls `POST /api/projects/{pid}/ai-run`; `main.ai_run()` then calls `runner.start_run()`.

`start_run()`:

1. Resolves current and prior documents.
2. Loads the checklist through `ai_review.load_config()`.
3. Selects current-edition tables with `_select_targets()`; listings and figures are not model-reviewed.
4. Acquires `_acquire_project_lease()`. The present lease is process-global, so mutating review runs are serialized.
5. Freezes the selected model, reasoning effort, and checklist signature.
6. Creates an `ai_run` row.
7. Initializes in-memory `RUN_PROGRESS`.
8. Starts background `_do_run()` in a daemon thread.

The UI polls `GET /api/projects/{pid}/ai-progress` through `pollProgress()`.

AI role: the API is not called yet. This stage selects and freezes configuration.

Design defense: run configuration is captured before the background thread begins so one run cannot accidentally observe another request's changing model or effort. The process-local lease is adequate only for this single-process prototype; a multi-worker deployment needs a database or distributed lease.

### 3.6 Preflight and rerun modes

`runner._do_run()` calls `ai_client.preflight()` before expensive work. The preflight performs a cheap model-list request with a short timeout. A connection outage aborts loudly and cannot become an empty clean result.

Rerun modes:

- `incremental`: reuse valid extraction and judgment caches.
- `fresh`: clear current-edition extraction/judgment caches but retain reusable prior-edition cache.
- `rebuild`: clear extraction/judgment caches for every edition.

Partial or failed runs demote system-owned `Auto-approved` states. They do not overwrite human-set statuses.

### 3.7 Per-table text and cache path

`_do_run()` submits `_analyze_table()` for each current table to a thread pool. `ai_client._create()` applies one global `MAX_INFLIGHT` semaphore to all model calls, so nested table and slice concurrency cannot exceed the configured gateway budget.

Inside `_analyze_table()`:

1. `_cached_extraction()` compares the source PDF's raw byte hash with stored `src_hash`. A verified complete cache hit avoids reading the PDF at all.
2. Otherwise `_page_texts_for()` extracts that table's pages once.
3. `_extraction_for()` hashes the full text and may reuse a matching `content_hash` cache.
4. If no valid extraction exists, it calls `ai_review.extract()`.

Persisted cache fields on `output`:

| Field | Meaning |
|---|---|
| `extraction_json` | Structured table transcription and coverage metadata |
| `content_hash` | Hash of the full extracted text |
| `src_hash` | Raw source-file byte hash; present only for a complete reusable extraction |
| `judge_key` | Fingerprint of current/prior content, model, effort, checklist, and judge-logic version |

Failed-read partial extractions may be stored so the reviewer can inspect available evidence, but they do not receive reusable completion stamps and a later incremental run retries the work. A deliberate slice-cap truncation may be cached as stable under that configuration, but its coverage flag still blocks cross-output review and complete-run status until the configuration permits full coverage.

AI role: only the final extraction call; hashing and cache decisions are deterministic.

### 3.8 Structured table transcription

`ai_review.extract()` converts page text into `EXTRACTION_SCHEMA` fields such as:

```json
{
  "analysis_set": "Safety Analysis Set",
  "header_label": "Table 2.1",
  "groups": [
    {"label": "Drug X", "n": 200},
    {"label": "Placebo", "n": 198}
  ],
  "footnote_markers": ["Treatment-emergent adverse events"],
  "summary_rows": [
    {
      "label": "Any TEAE",
      "values": {"Drug X": 191, "Placebo": 187},
      "pcts": {"Drug X": 95.5, "Placebo": 94.4},
      "page": 1,
      "section": "Adverse Events",
      "row_kind": "aggregate"
    }
  ],
  "missing_n_rows": [],
  "coverage": {
    "pages_total": 1,
    "pages_read": 1,
    "incomplete": false,
    "truncated": false
  }
}
```

Important functions:

- `_extract_once()` handles a small output in one structured call.
- `_slices()` splits large outputs by page and approximately 4,500-character blocks.
- `_extract_paged()` sends the first slice alone to learn exact group labels, processes later slices concurrently, and merges results in source order.
- `_merge_into()` stamps the known source page instead of trusting a model-estimated page.
- `_validate_extraction_response()` rejects malformed objects, group denominators, rows, and field types.

By default, mechanical transcription uses `FAST_MODEL` at low effort; the reviewer's chosen reasoning model remains available for judgment. `TLF_FAST_EXTRACT=0` disables that split.

AI role: copy table facts into a constrained schema. It is told not to judge, calculate, or invent.

Design defense: extracting first separates perception/transcription from reasoning. It gives later judges a compact common representation, supports caching, and lets Python inspect coverage and arithmetic. However, schema validity does not prove that every printed row was captured correctly.

### 3.9 Precision guard 1: suspect-cell self-check

`suspect_cells()` deterministically inspects the transcription. For a cell with count `n`, denominator `N`, and displayed percent `p`, it checks:

```text
n <= N
abs(round(n / N * 100, 1) - p) <= configured tolerance
```

`needs_self_check()` decides whether any suspect exists without reading the PDF again. Only then does `self_check()` send the suspect cell identifiers and source text to a focused model call that rereads exact printed values. It patches the extraction but does not ask the model to decide whether the source table is correct.

AI role: focused rereading of suspect cells.

Deterministic role: identify suspects and decide whether the reread is needed.

Important limitation: this catches internal inconsistencies in the extracted fields; it is not a proof that every apparently consistent extraction equals the PDF. A schema-valid empty correction list is accepted, so the self-check also does not prove that every flagged suspect was reconciled.

### 3.10 Within-table and prior-version judgment

If a prior edition contains a matching table label, `_analyze_table()` obtains its extraction. Failure to extract a matched prior table makes version review incomplete rather than silently omitting the comparison.

`ai_review.within_table_judge()` builds one prompt containing:

- current structured extraction;
- matching prior extraction when available;
- only applicable checklist items from `study_config.json`;
- a required structured finding schema.

It covers current within-table items such as sums and `n <= N`, and folds version items into the same call when prior evidence exists.

A finding returned by the model resembles:

```json
{
  "checklist_item": "2.1",
  "risk": "High",
  "message": "Cohort counts 42 and 38 do not equal printed total 83.",
  "cited_numbers": [42, 38],
  "operation": "sum_equals",
  "observed": 83,
  "page": 1,
  "printed_page": 1,
  "pages_total": 1,
  "section": "Safety Population",
  "row_kind": "aggregate",
  "subjects": []
}
```

`_judge_schema()` constrains the structure. `_build_judge_findings()` rejects unknown checklist item identifiers and normalizes the result to application fields such as `AIW-2.1`, `AIV-6.2`, risk, evidence numbers, location, and affected outputs.

`within_table_findings()` also turns model-extracted `missing_n_rows` into `FMT-002` candidates; the model supplied the extracted fact, while Python formats the finding.

AI role: decide applicability and whether structured evidence constitutes a checklist discrepancy.

Python role: choose applicable checklist items, enforce schema and identifiers, normalize values, and reject malformed output.

### 3.11 Precision guard 2: cited-arithmetic verification and deduplication

`ai_review.verify_findings()` calls deterministic `_contradicts()` for supported operations:

- `sum_equals`: cited addends should equal `observed`;
- `equals`: the first two cited values should match;
- `less_equal`: keep a discrepancy only when the first cited value exceeds the second;
- `decreased`: keep a discrepancy only when the current value is below the prior value;
- `increased`: keep a discrepancy only when the current value is above the prior value;
- `none`: qualitative claim, not arithmetically verifiable.

If a numeric finding's own cited values do not support its claim, Python drops it. `checks.dedupe()` then collapses near-duplicates by family, page, numeric evidence, and normalized message prefix.

AI role: none.

Critical limitation: this verifies the arithmetic of the numbers returned by the model. It does not independently re-anchor every cited number to the source PDF. Qualitative findings pass through for human judgment because Python lacks a sound arithmetic test for them.

### 3.12 Durable within-table publication

`_replace_output_within_findings()` starts an immediate SQLite transaction, deletes that output's old `within` phase, inserts the new findings, and stamps `judge_key` last. A crash before the stamp causes a later run to rejudge the table rather than treating partial work as complete.

When `judge_key` already matches, `_analyze_table()` keeps the existing findings and states. A forced or invalidated rejudge recreates findings as `pending`; historical human decisions remain in `review_log`, but the application does not yet automatically rematch and reapply those decisions.

### 3.13 Cross-output judgment

Cross-output review runs only if every target table has usable, nontruncated extraction and successful table judgment.

`_bundle_entry()` projects each extraction to groups, footnotes, and body rows. `_chunk_bundle()`:

- groups sibling table families so a comparison is not split across calls;
- repeats `Table 1` as a hub in every chunk for population/by-study reconciliation;
- limits ordinary chunks by character budget;
- fails visibly rather than silently splitting an oversized family.

Each chunk goes to `ai_review.cross_output_judge()`, which covers configured cross-output checklist items such as population totals, by-study rows, footnotes, AE overview/detail reconciliation, and by-study versus summary outputs.

The resulting findings are verified, deduplicated, mapped to a primary output and all `affected` labels, then transactionally published as phase `cross` by `_replace_phase_findings()`.

AI role: semantic and numeric reconciliation across extracted tables.

Python role: completeness gate, context construction, family-preserving chunking, arithmetic verification, deduplication, output mapping, and atomic publication.

Checklist items marked `requires_multi_document_upload` are explicitly excluded from this judge. The public configuration's cross-document item 9 is therefore a roadmap, not implemented review coverage.

### 3.14 Completion and automatic status policy

A run is complete only if all of the following hold:

```text
all selected tables extracted
AND all table judges completed
AND no extraction failure
AND no judge failure
AND no truncated output
AND every extraction has usable groups and numeric rows
AND cross-output phase succeeded
AND structural phase succeeded
AND no accumulated error
```

Only after that does Python compute `_blocked_output_ids()`. It blocks both the finding's primary output and every output named in `affected`. The block is conservative and currently includes rejected findings as well as pending or posted findings. Current tables still in `Not Reviewed` and not implicated by any finding may become `Auto-approved`; rejecting a candidate does not itself cause system auto-approval, although a reviewer may set `Manually approved`.

Partial or failed runs call `_demote_autoapproved()` and cannot create a clean result. Human states such as `Manually approved`, `In Progress`, and `Needs Revision` are not overwritten.

AI role: none. The model proposes candidates; Python owns completion and status policy.

Design defense: zero findings is meaningful only when the system can prove that every required stage completed with usable evidence.

### 3.15 Single-output rerun

`runner.run_single_output()` clears and recomputes one table's extraction and within/version findings. It leaves cross-output findings untouched, demotes any system-owned approval, and does not automatically approve the table. A full run is required to refresh global cross-output conclusions. The single-output success check is narrower than the full-run gate: a schema-valid but empty extraction can finish this diagnostic rerun, which is safe from auto-approval but remains a prototype gap.

## 4. Model client and structured-output mechanics

`ai_client.py` is the only model-transport layer.

Important functions:

- `available()` requires the SDK and API key, but always returns false in `TLF_DEMO_MODE`.
- `_client()` creates the Anthropic SDK client and honors an optional compatible base URL.
- `_discover_models()` attempts to list models available to the current key and de-duplicates aliases. If discovery fails, the UI uses a curated fallback list, so the list is not proof that every displayed model is granted to that key.
- `configure()` and `run_config()` manage selected model and effort.
- `_thinking()` applies adaptive reasoning only to model families that support it.
- `_create()` applies the global in-flight semaphore and falls back to streaming only when a long nonstreaming request is rejected.
- `call_structured()` supplies a tool/input schema. It first permits an adaptive-thinking auto-tool response where supported, then uses a forced-tool fallback. Missing, truncated, malformed, or invalid downstream output is an error rather than an empty clean response.
- `call_text()` is used by chat rather than review extraction/judgment.
- `_log_usage()` records model and token usage locally.

Why tool-based structured output: downstream verification, persistence, filtering, and source linkage require typed fields rather than prose that would need another unreliable parser.

Despite comments in `runner.py` that refer to per-context configuration, the checked implementation stores selected model and effort in module-level variables. The process-global review lease and repeated application of the same frozen run configuration prevent conflicting mutating runs in this single-process prototype; this is not true `ContextVar` isolation and is another reason not to claim multi-worker readiness.

## 5. Persistence and human workflow

### 5.1 SQLite schema and concurrency

`db.py` stores state in `$TLF_DATA_DIR/app.db`. Uploaded files live below `$TLF_DATA_DIR/uploads/{project_id}`.

Core tables:

| Table | Role |
|---|---|
| `project` | Project metadata |
| `document` | Current, prior, TOC, SAP, protocol, or SPP file metadata |
| `output` | Indexed TLF output, page range, status, extraction and judgment cache |
| `finding` | Structural, within, cross, or imported review candidate and state |
| `comment` | Human comments, replies, and finding-linked posted comments |
| `annotation` | Highlight, rectangle, or freehand PDF geometry |
| `ai_run` | Review start/end time and completion/error/coverage summary |
| `audit_log` | Operational action history |
| `review_log` | Context-rich human decisions captured for possible future learning |

`db.get()` gives each thread its own connection, enables foreign keys and WAL mode, and sets a busy timeout. This supports concurrent table workers while retaining SQLite's single-writer semantics.

`review_log` is capture-only. No current code fine-tunes a model, changes a prompt, or derives rules from it.

### 5.2 Finding states and human authority

The UI renders finding cards in `static/app.js` and calls `main.finding_action()`:

- `post`: creates a linked comment, marks the finding `posted`, and moves an untouched output to `In Progress`;
- `reject`: marks it `rejected`;
- `reopen`: returns it to `pending` and removes its linked posted comment.

`db.log_finding_action()` records the decision context, checklist item, evidence, location, reviewer text, and stable finding signature in `review_log`.

`main.set_status()` accepts only human-settable statuses. `Auto-approved` is deliberately absent from the UI's status choices.

Important nuance: resolving a comment changes `comment.resolved`; it does not currently change the linked finding to `resolved`. Human-set output status and decision history persist, but a forced rejudge recreates current findings as pending instead of automatically reapplying old decisions.

The logs are audit-oriented application records, not cryptographically immutable or exhaustive regulated audit evidence. Some actions are captured in `audit_log`, some human decisions in `review_log`, and several convenience actions are not represented in both. Project deletion also removes that project's logs.

### 5.3 Comments, annotations, chat, and viewer

- `main.tlf_clip()` and `pdftools.clip()` supply only the selected output's PDF range to PDF.js.
- `main.add_comment()`, `reply_comment()`, and `resolve_comment()` manage discussion threads.
- `project_io.validate_annotation_payload()` bounds and canonicalizes annotation geometry before `main.add_annotation()` stores it.
- `chat.ask_output()` sends one output's stored extraction and findings to the model.
- `chat.ask_global()` sends the project output list and finding digest, not every output's raw extraction.
- `_study_doc_blocks()` can attach SAP/protocol text as ephemeral prompt-cached system blocks.

AI role in chat: answer questions from supplied project context. Chat does not modify findings.

Limitations: chat does not anonymize free text. DOCX reference files are accepted at upload, but the present chat text reader is PDF-oriented; failures are swallowed, so DOCX reference content is not reliably available. Reference-document use should be described as incomplete.

## 6. Import and export

### 6.1 Excel and annotated PDF

`export.py` provides:

- `comments_xlsx()`
- `findings_xlsx()` and summary sheet generation
- `annotated_pdf()`
- `import_findings_xlsx()`
- `import_comments_xlsx()`

`_excel_value()` prefixes formula-leading strings so untrusted text remains text when a workbook opens. Import functions validate headers, values, identifiers, and supported sheets before applying data. Comment and finding imports then write rows through committing database helpers rather than one explicit all-or-nothing transaction, so a rare write-time failure could leave partial changes; the portable project import described below does use an explicit database transaction.

Imported findings are additive and use phase `imported`; copied deterministic findings are skipped because the application regenerates them. The imported-results convenience path does not have the same extraction-coverage proof as an in-app full review and should not be described as equivalent validation.

The import convenience path can auto-approve untouched outputs based on imported rows without the stricter full-run coverage gate. Unmatched or cross-output rows make that shortcut especially less authoritative than an in-app completed review.

### 6.2 Portable project bundle

`project_io.export_bundle()` writes a ZIP containing `project.json` plus project files. Absolute workstation paths are removed from the manifest.

`project_io.import_bundle()` validates:

- entry count;
- duplicate member names;
- encryption and compression methods;
- symbolic links;
- per-entry and total expansion;
- compression ratio;
- exact member paths;
- strict JSON, table schemas, IDs, enums, and JSON-valued fields;
- supported document formats.

It remaps old IDs to new IDs and imports the project in one SQLite transaction.

Although absolute workstation paths are removed, the bundle still contains source documents, cached extraction data, findings, comments, annotations, and logs. It can therefore contain sensitive content and must be handled as project data, not as a sanitized public artifact.

AI role: none.

## 7. Frontend map

The browser client is `static/app.js`, with layout in `static/index.html`, styling in `static/styles.css`, and local PDF rendering through vendored PDF.js.

Important frontend functions:

| Function | Responsibility |
|---|---|
| `boot()` | Load runtime flags and initial page |
| `renderHome()` | Project list and create/import interface |
| `openProject()` | Load full project state |
| `renderDashboard()` | Summaries and progress |
| `renderTOC()` | Indexed output tree and review statuses |
| `renderTLF()` / `selectOutput()` | PDF viewer and selected output |
| `drawAnnotations()` / `saveAnnotation()` | Local annotation display and persistence |
| `renderAI()` / `loadFindings()` | AI-review controls, run summary, and finding queue |
| `startRun()` / `pollProgress()` | Start background review and poll progress |
| `findingCard()` / `act()` / `postFinding()` | Human adjudication |
| `sendOutputChat()` / `sendGlobalChat()` | Context-scoped model chat |
| `renderComments()` | Project comments and threads |

Dynamic strings inserted into HTML are escaped through `esc()`, and status/finding CSS classes come from fixed maps rather than arbitrary server values.

## 8. One-click synthetic demo

`run_synthetic_demo.sh` creates or reuses a local virtual environment, installs the pinned demo dependencies, and invokes `demo/run_demo.py`. The environment may be reused, but each launch receives a new timestamped/PID-scoped data directory.

`demo.run_demo.prepare()`:

1. Sets an isolated `TLF_DATA_DIR` and `TLF_DEMO_MODE=1`.
2. Generates two fictional, watermarked 12-page PDFs.
3. Initializes a separate SQLite database.
4. Inserts 12 current and 12 prior output records.
5. Inserts prebuilt extraction JSON.
6. Inserts 11 pre-seeded findings and a completed simulated `ai_run`.
7. Seeds one posted and one rejected decision, a comment, and audit records.
8. Writes SHA-256 hashes in `synthetic_manifest.json`.
9. Starts Uvicorn on loopback unless `--prepare-only` is selected.

`ai_client.available()` and `_client()` explicitly disable external AI in demo mode even if the launching shell contains a key.

The demo lets a reviewer exercise the interface, state transitions, persistence, and exports. It does not execute the extraction or judgment pipeline or the real auto-approval policy: it seeds finding-bearing tables as `In Progress`, clean distractors as `Not Reviewed`, and a simulated successful run with zero auto-approvals. Automated demo contracts are covered separately in `tests/test_synthetic_demo.py`.

## 9. Synthetic evaluation workflow

### 9.1 What it evaluates

The evaluation is a synthetic fault-injection benchmark for evaluation and safeguard mechanics. It does not evaluate Claude, PDF extraction on real documents, clinical accuracy, or regulatory fitness.

Default corpus:

- 50 fictional projects;
- 10 current and 10 prior one-table pages per project;
- 1,000 pages;
- 17 finding families;
- 850 opportunities;
- exactly 10 planted positives per family, or 170 truth findings;
- 680 clean opportunities;
- 120 high-risk and 50 low-risk findings.

### 9.2 Generator and truth separation

`evaluation/catalog.py` defines version, seed, family taxonomy, risk, scope, operation, and detector group.

`evaluation/generate.py` provides:

- `stable_int()` and `stable_uniform()` for order-independent seeded behavior;
- `_evidence()` for clean or violating structured evidence;
- `evidence_is_violation()` as an independent rule-based reconstruction of the label;
- `generate_dataset()` for predictor-visible `cases` and scorer-only `truth`;
- `validate_dataset()` for published invariants and truth/evidence agreement.

Predictor-visible opportunity:

```json
{
  "opportunity_id": "SYN-P032:AIX-7.1",
  "project_id": "SYN-P032",
  "family": "AIX-7.1",
  "risk": "High",
  "operation": "equals",
  "detector_group": "cross_output",
  "difficulty": "easy",
  "locator": {
    "output_label": "Table 8",
    "row": "SYNTHETIC ROW 13",
    "column": "Drug X",
    "comparison_output": "Table 9"
  },
  "evidence": {"left": 191, "right": 187}
}
```

The corresponding truth record is stored separately, and no `is_issue` field is exposed in the opportunity record. However, the simulator calls `evidence_is_violation()` on the predictor-visible structured evidence and therefore reconstructs whether the opportunity is clean or positive before applying its detection or false-positive probability. It is an oracle-conditioned behavioral generator over structured opportunities, not a realistic blind predictor and not a PDF reader.

### 9.3 Three configurations

`evaluation/systems.py` defines:

1. `structural_rules()`: three deterministic structural families only.
2. `simulated_llm_raw()`: a seeded behavioral proxy with explicit detection, false-positive, difficulty, duplicate, and numeric-citation-corruption assumptions.
3. `run_systems()`: rules-only, simulated-model-only, and guarded-hybrid arms.

For a planted issue:

```text
P(emit) = base detection probability for detector group
          * difficulty multiplier
```

For a clean opportunity, the system uses the configured false-positive probability. Hash-derived random values make every decision reproducible for the same seed and opportunity ID.

The guarded hybrid receives the exact same raw simulated predictions as the model-only arm, then bundles `verify_predictions()`, structural rules, `dedupe_predictions()`, simulated extraction self-check recovery, and coverage gating. The paired comparison estimates the combined configured hybrid-minus-proxy difference; it is not a guard-by-guard causal ablation. Structural-rule union, verification, and deduplication affect finding metrics. Simulated self-check affects extraction statistics, while the simulated coverage gate affects auto-approval eligibility rather than the prediction list.

The opportunity-level `coverage_complete` field is not used to gate prediction or scoring. Operational page coverage is simulated separately in `_operational_stats()` with its own configured miss probability.

### 9.4 Matching and metrics

`evaluation/scoring.py` provides:

- `finding_key()`: project, family, output, row, column, comparison output;
- `one_to_one_match()`: exact structured matching; message wording is ignored and a truth item can be used only once;
- `_project_record()`: TP, FP, FN, high-risk and table-safety counts per project;
- `aggregate_metrics()`: precision, recall, F1, macro-F1, high-risk recall, false positives per 100 tables, clean-table specificity, issue-table miss rate, unsafe auto-approval, coverage, simulated extraction, usage, and latency;
- `bootstrap_metrics()`: 2,000 paired percentile resamples of whole projects;
- `score_all()`: aggregate results and hybrid-minus-baseline comparisons.

Metric denominators are deliberately explicit:

- false positives per 100 tables uses the 500 current synthetic tables;
- clean-table specificity is clean truth tables with no prediction divided by all clean truth tables;
- issue-table miss rate is issue-bearing truth tables with no prediction divided by all issue-bearing truth tables;
- unsafe auto-approval is issue-bearing eligible auto-approved tables divided by all eligible auto-approved tables.

In these definitions, a predicted-clean table means only that no prediction was emitted; it is not independently verified correctness.

Whole-project resampling preserves dependence among pages and findings in one fictional delivery. The same sampled indices from the 50 realized projects are applied to every arm, so system differences are paired.

These percentile intervals are descriptive resampling uncertainty for the fixed realized synthetic benchmark and simulator. They do not capture real prevalence, model-call stochasticity, generator uncertainty, or external generalization, and they are not confidence intervals for real model performance.

Arithmetic verification can remove true as well as false predictions when the simulator corrupts cited operands. Deduplication removes both repeated proxy candidates and overlaps between deterministic structural rules and proxy findings; its count should not be interpreted as only the simulator's explicit duplicate process.

### 9.5 Artifact generation

`evaluation.run_benchmark.run()` executes generation, systems, scoring, and reporting. It writes:

- `cases.jsonl` and `truth.jsonl`;
- one prediction JSONL per configuration;
- one exact matching file per configuration;
- aggregate and per-project metrics;
- paired comparisons;
- dataset card, taxonomy, and benchmark configuration;
- GitHub-readable `REPORT.md` and self-contained HTML;
- `artifact_hashes.txt` for deterministic outputs;
- separate machine-dependent `runtime_environment.json`.

`reproducibility_record()` binds the report to the hash of the six benchmark source files named in `BENCHMARK_SOURCE_FILES` and to a separate dependency-lock hash. It does not hash the whole application, tests, `ai_review.py`, or `checks.py`. `artifact_hashes.txt` covers deterministic benchmark artifacts; machine-dependent runtime metadata is intentionally outside that deterministic hash set. The reference configuration also copies the generated Markdown report to `evaluation/REPORT.md`.

## 10. How a real-model evaluation would differ

The current normal application stores live model findings in SQLite. The checked benchmark expects JSONL prediction records. A future governed adapter would:

1. Freeze a document set and independent expert-adjudicated truth.
2. Freeze exact model ID, provider, prompt/checklist, effort, code commit, and decoding/configuration.
3. Run the normal structured extraction and judging pipeline without exposing truth.
4. Define and validate an adapter from live findings to the synthetic scorer's exact family/output/row/column/comparison locator key; the schemas are not directly interchangeable.
5. Normalize locators under predeclared adjudication rules and export predictions without exposing truth.
6. Run the same one-to-one scorer and project-level bootstrap.
7. Preserve raw model responses, errors, coverage, and run manifest under appropriate access controls.

The model must not create or see the reference truth. Tuning must use separate project-level development and validation sets; an untouched project-level test partition is needed for a final estimate. A defensible study would also use blind multi-reviewer expert adjudication, lock model/prompt/configuration hashes, account for failures and coverage, and repeat calls if model nondeterminism is an estimand.

## 11. Script and module reference

| File | Main responsibility | AI involved? |
|---|---|---:|
| `main.py` | FastAPI routes, upload/request boundary, review endpoints, comments, findings, annotations, static SPA | Calls orchestration/chat; does not itself reason |
| `runner.py` | Review lifecycle, concurrency, caching, phase completion, persistence, auto-approval | Orchestrates model stages and deterministic guards; it is the workflow state machine |
| `ai_client.py` | Anthropic-compatible transport, model discovery, effort, structured output, token logging, concurrency cap | Yes |
| `ai_review.py` | Extraction schema/prompt, slice merging, self-check, checklist judges, model-output validation, arithmetic verification | Mixed |
| `checks.py` | Structural findings, signatures, deduplication | No |
| `indexer.py` | Bookmark/header indexing and caption reconciliation | No |
| `pdftools.py` | PDF validation, text, clipping, hashing, page counts | No |
| `db.py` | SQLite schema, migrations, queries, audit and review logs | No |
| `chat.py` | Build output/project context and ask model questions | Yes |
| `export.py` | XLSX and annotated-PDF import/export | No |
| `project_io.py` | Portable ZIP validation, export/import, ID remapping | No |
| `static/app.js` | Browser state, dashboard, viewer, review controls, API calls | No local inference |
| `demo/run_demo.py` | Generated PDFs and pre-seeded isolated demo database | No; external AI disabled |
| `evaluation/catalog.py` | Synthetic taxonomy and constants | No |
| `evaluation/generate.py` | Cases, truth, and invariant validation | No |
| `evaluation/systems.py` | Rules baseline, behavioral simulator, simulated guards | No real AI |
| `evaluation/scoring.py` | Exact matching, metrics, cluster bootstrap | No |
| `evaluation/report.py` | Markdown, CSV, and HTML rendering | No |
| `evaluation/run_benchmark.py` | Evaluation entry point, artifacts, hashes | No |
| `public_release.py` | Public-source privacy/content boundary and exact-tree audit | No |

## 12. Tests and what they defend

| Test area | Examples of defended behavior |
|---|---|
| `tests/test_api.py`, `test_main_document.py` | Project lifecycle, upload limits, filename safety, document validation |
| `tests/test_indexer.py`, `test_indexer_safety.py` | Bookmark/header recovery, caption correction, parser bounds |
| `tests/test_ai_review.py` | Extraction validation, suspect cells, verification, schema behavior |
| `tests/test_runner_parallel.py` | Actual concurrency, ordering, containment, coverage, cache/resume, fail-closed runs |
| `tests/test_checks.py` | Structural checks and deduplication |
| `tests/test_comments_roundtrip.py`, `test_findings_import.py` | XLSX import/export behavior and spreadsheet safety |
| `tests/test_project_io.py` | ZIP traversal/bomb/schema defenses, remapping, privacy-safe export |
| `tests/test_frontend_safety.py` | CSP hashes, escaping, safe frontend contracts |
| `tests/test_review_log.py` | Human-decision evidence capture |
| `tests/test_synthetic_demo.py` | Demo isolation, fictional content, no external calls |
| `evaluation/tests/test_benchmark.py` | Corpus invariants, separate truth stream, exact duplicate-penalizing matching, guard mechanics, bootstrap output, deterministic artifacts, source/lock binding |
| `tests/test_public_release.py` | Required files, secrets/identifiers, exact media hashes, staged/history boundary |

## 13. Design questions and defensible answers

### Why not ask one model to read the PDF and return a final verdict?

The staged architecture exposes evidence and failure modes. Structured extraction can be cached and coverage-audited; judges receive compact data; arithmetic can be recomputed; duplicate findings can be removed; and a human can inspect the linked source. A monolithic answer would make missed pages, fabricated numbers, and unsupported conclusions harder to detect.

### Is this a free-form autonomous AI agent?

No. It is better described as a deterministic workflow orchestrator around several bounded model calls. `runner.py` fixes the state machine, allowed stages, context, retries, concurrency, completion gates, and database writes. The model cannot choose arbitrary application tools or alter the workflow; provider-side tool use is used only to serialize a response into the required schema.

### Why use a model for extraction if extraction errors are possible?

Dense TLF text varies enough that fixed parsing alone is brittle. The model is used for flexible schema transcription, but it is constrained, validated, covered page by page, and selectively reread. This is a prototype trade-off, not a claim that model extraction is ground truth.

### Why not implement all arithmetic checks directly in Python?

Python is preferable once the relevant operands and comparison contract are known. The hard part is often recognizing which rows, treatment groups, populations, editions, and outputs are comparable. The model proposes that semantic alignment and cites operands; Python then verifies supported arithmetic. More deterministic checks should be added whenever stable specifications make them possible.

### What does arithmetic verification prove?

It proves only that the finding's cited numbers satisfy the stated discrepancy operation. It does not prove that the cited numbers were copied from the correct PDF cells or that the comparison is clinically applicable. Source review and better grounding remain necessary.

### Why is full coverage required before auto-approval?

A missing finding is informative only if every required page and review phase completed with usable evidence. Otherwise absence of output may simply mean the model never saw the relevant page or a call failed.

### How are long deliveries kept within context limits?

Extraction is page-sliced. Cross-output data is projected to structured rows, grouped by output family, divided by character budget, and supplied with Table 1 repeated as a hub. Oversized comparison families fail visibly rather than being silently separated.

### How are reruns made efficient and stable?

Raw file hashes, full-text hashes, structured extraction JSON, and judge fingerprints avoid repeated work. Run configuration is frozen. Within findings are committed table by table, while project-level phases are transactionally replaced. Failed-read partials never receive completion stamps; cap-truncated coverage may be cached but cannot complete the run.

### What happens when the AI service is unavailable?

Preflight detects connection outages early. Connection failures abort loudly, stale auto-approvals are revoked, and `review_complete` remains false. Other per-table errors are contained and reported, but any such error also prevents complete-run status.

### Is the system learning from reviewer decisions?

Not currently. It records structured reviewer actions in `review_log` for future analysis. A future tuning process could derive prompt/rule candidates on development data, but the present application does not change its model or behavior automatically.

### Is the checked evaluation an AI evaluation?

No. It is a reproducible simulator-based evaluation of the scoring framework and modeled safeguard effects. A real AI evaluation needs locked model calls and independent expert truth on governed documents.

### Is it production-ready or validated for regulated use?

No. It is a local, single-user prototype. It lacks authentication, authorization, tenant isolation, application encryption, regulated change control, formal computer-system validation, and real-data clinical performance evidence.

## 14. Important limitations and implementation gaps

- Model review currently targets tables; listings and figures are indexed but skipped.
- There is no OCR pipeline for scanned or image-only pages.
- Page coverage does not guarantee semantic extraction completeness.
- Blank-page detection is parser-dependent and best effort; failure to obtain character counts is not surfaced as a blank-page finding.
- Cross-document checklist item 9 is gated out.
- TOC and SPP uploads are stored but not operational review inputs.
- SAP/protocol context is used by chat, not the judges; DOCX text support is incomplete.
- The process-local lease and in-memory progress map do not support a multi-worker server deployment.
- `review_log` supports future learning analysis but no implemented tuning.
- Forced rejudging does not automatically rematch old human decisions to new findings.
- Comment resolution does not automatically resolve the linked finding.
- Rejected findings still conservatively block system auto-approval.
- The findings-clear convenience route deletes every project finding phase, not only AI candidates, and does not itself recompute statuses or add a complete audit record.
- Imported findings do not carry the same in-app extraction-coverage proof as a full review.
- Excel row application is not fully transactional, whereas portable project import is.
- Audit and review logs are useful history, not immutable or exhaustive regulated audit evidence.
- This application must remain on loopback and must not process confidential or regulated data in its portfolio configuration.

## 15. A concise interview walkthrough

Use this 90-second version:

> "The application is a local FastAPI and SQLite review workspace. On upload, Python validates the files, indexes output page ranges from bookmarks reconciled against printed captions, and immediately runs deterministic blank-page, missing-output, and numbering checks. For optional model-backed review, the runner freezes the model, effort, and checklist, then analyzes current tables concurrently under a global API-call limit. The first model stage transcribes each table into a typed schema with groups, rows, counts, percentages, footnotes, and coverage. Python detects internally inconsistent extracted cells and asks a focused model call to reread only those cells. A second structured model stage applies within-table and prior-version checklist items, while a later chunked stage compares outputs across the delivery. Every numeric candidate must cite its operands and operation; Python recomputes that arithmetic and removes unsupported or duplicate findings. A table is auto-approved only if all tables and phases complete with usable, nontruncated coverage and no finding implicates it. The reviewer remains authoritative through source-linked post, reject, reopen, comments, annotations, statuses, and exports. The public demo is pre-seeded and offline, while the published evaluation uses a seeded simulator rather than Claude, so its metrics demonstrate the evaluation and safeguard design, not clinical model accuracy."

## 16. Suggested reading order before an interview

1. Read this guide once end to end.
2. Read `README.md` for the public claim boundary.
3. Trace `main.create_project()` and `runner.start_run()`.
4. Trace `runner._analyze_table()` and `ai_review.extract()`.
5. Trace `within_table_judge()`, `verify_findings()`, and `_chunk_bundle()`.
6. Trace the completion condition around `review_complete` and auto-approval.
7. Read `evaluation/README.md` and `evaluation/REPORT.md`.
8. Inspect one `cases.jsonl`, prediction JSONL, matching JSON, and `metrics.json` record.
9. Review the limitations above and practice stating them without overclaiming.
