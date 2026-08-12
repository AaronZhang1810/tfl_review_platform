# Synthetic offline demo

This demo creates a new isolated SQLite database and two fictional, watermarked 12-page TLF PDFs on every launch. It seeds 11 auditable discrepancies, three clean distractor tables, simulated review output, one accepted finding, one rejected finding, a reviewer comment, and an audit trail.

> **SYNTHETIC DEMO — no real clinical or patient data; AI behavior is simulated.**

It does not need an API key and external AI calls are hard-disabled in demo mode. It binds only to `127.0.0.1` and never reads or writes the normal `data/` directory.

From the repository root:

```bash
./run_synthetic_demo.sh
```

Generate the PDFs/database/manifest without launching the server:

```bash
python demo/run_demo.py --prepare-only
```

Every run writes under `demo/artifacts/runs/<timestamp>-<pid>/`. The printed manifest records the document/database SHA-256 hashes and the fixed seed.

Prepared, reviewed portfolio collateral is under `docs/`:

- `docs/images/` — dashboard, TOC, source-linked findings, adjudication, and benchmark
- `docs/media/tlf-review-demo.mp4` — exactly 120 seconds, 1920×1080 H.264, with burned-in explanatory captions and synthetic-result disclosures

Regenerate the captioned video after updating screenshots with:

```bash
./demo/build_video.sh
```

The timed narration/capture plan is in `DEMO_VIDEO_SCRIPT.md`; the burned-in captions are in `video_captions.srt`.
