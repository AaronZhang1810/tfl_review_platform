# Setup for the Synthetic TLF Review Demonstration

This guide covers the source and containerized portfolio demonstration. It does not
authorize publication or use with real clinical documents.

## Prerequisites

- Python 3.11 or 3.12, or Docker with Compose
- A modern browser
- No API key for document browsing, deterministic checks, comments, or exports
- An optional Anthropic API key for AI review and chat

When AI is enabled, relevant TLF text, structured extractions, and optional
reference-document text leave the local machine and are sent to the configured
provider. Use only the generated fictional documents. Review [PRIVACY.md](PRIVACY.md)
before enabling AI features.

## Source installation

```bash
python -m venv .venv
source .venv/bin/activate                 # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements-lock.txt
cp configs/study_config.synthetic.json study_config.json
```

`requirements-lock.txt` is the resolved Python 3.12 development environment used
by CI and the public regression run. `requirements-demo-lock.txt` is the smaller
exact runtime used by the one-click launcher and Docker. The non-lock requirement
files retain reviewed version ranges for deliberate dependency upgrades.

Both lock files pin exact versions but do not include package-distribution hashes.
They make version resolution repeatable, not byte-for-byte package acquisition. A
release process should install from a reviewed index or artifact mirror and either
verify distributions independently or adopt a reviewed hash-locked workflow.

Generate the fictional demo and start its isolated local server:

```bash
python demo/run_demo.py
```

Open <http://127.0.0.1:8765>. Keep the host set to `127.0.0.1`; this edition has
no authentication or user isolation. The demo writes to a timestamped directory
under ignored `demo/artifacts/`, never to normal application data.

## Run the full local application on macOS

The one-click demo above starts a pre-seeded, offline showcase. To launch the normal
source application—with a persistent workspace and uploads enabled—run these
commands from the repository root after completing **Source installation**:

```bash
unset TLF_DEMO_MODE
export TLF_DATA_DIR="$PWD/data"
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. Without an API key, document browsing, deterministic
checks, reviewer statuses, annotations, comments, imports, and exports remain
available; model-backed review and chat report that AI is unavailable. Stop the
server with `Control-C`.

Keep the service on loopback. This source mode writes uploaded documents, extracted
content, and the SQLite database under `TLF_DATA_DIR`; it is therefore intentionally
separate from the isolated generated demo runs.

## Optional external-AI review

Set `ANTHROPIC_API_KEY` only in the shell or a secrets manager. Do not put a key in
source, a Dockerfile, a compose file, screenshots, or exported projects.

```bash
export ANTHROPIC_API_KEY="your-key-from-a-secret-store"
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

This mode calls the configured endpoint at runtime through the Anthropic Python SDK.
If `ANTHROPIC_BASE_URL` is set, the SDK uses that compatible endpoint. Model
availability, data handling, retention, and pricing depend on the configured provider
and account. The public demo makes no representation about those terms.

## Local safety limits

The source edition rejects oversized requests, individual documents, aggregate
project uploads, Excel imports, and project bundles. ZIP imports also enforce
entry-count, per-entry expansion, total expansion, duplicate-name, and compression-
ratio guards. Defaults are suitable for a local demonstration and can be adjusted
only after review with these positive-integer byte-count environment variables:

- `TLF_MAX_DOCUMENT_BYTES`
- `TLF_MAX_PROJECT_UPLOAD_BYTES`
- `TLF_MAX_PROJECT_FILES`
- `TLF_MAX_BUNDLE_BYTES`
- `TLF_MAX_SHEET_BYTES`
- `TLF_MAX_REQUEST_BYTES`
- `TLF_ZIP_MAX_ENTRIES`, `TLF_ZIP_MAX_ENTRY_BYTES`, `TLF_ZIP_MAX_TOTAL_BYTES`
- `TLF_ZIP_MAX_COMPRESSION_RATIO`

Raising a limit does not make an untrusted public deployment safe; authentication,
authorization, malware scanning, rate limiting, and a reviewed threat model remain
out of scope for this local prototype.

## Docker

Build, generate fictional data inside the Docker-managed demo volume, and run:

```bash
docker compose -f compose.demo.yml build
docker compose -f compose.demo.yml run --rm app \
  python demo/run_demo.py --prepare-only --data-dir /app/demo-data/current
docker compose -f compose.demo.yml up
```

The compose configuration is intentionally loopback-only, drops Linux capabilities,
uses a read-only root filesystem, and leaves only the `/app/demo-data` named volume
and the `/tmp` tmpfs writable. Remove the generated volume with
`docker compose -f compose.demo.yml down -v` when appropriate.

## Verification

```bash
python -m ruff check .
python -m pytest
python -m coverage run -m pytest
python -m coverage report
python -m pip_audit --local
python public_release.py --check
python public_release.py --check --staged --history
```

The dry release audit does not create an artifact. The staged/history form audits
exactly what would be committed and all reachable repository history.

## Local data and cleanup

Application state and uploaded files live under `data/`, which is excluded from Git,
Docker build context, and public release packages. The working `study_config.json`
is also ignored. A public package receives the fictional configuration from
`configs/study_config.synthetic.json`.

Deleting `data/` removes local demo projects and cannot be undone. Make a copy first
if you want to retain a generated run.

## Common problems

| Symptom | Resolution |
|---|---|
| `demo/run_demo.py` cannot import ReportLab | Install `requirements-dev.txt` or `requirements-demo.txt`. |
| `study_config.json` is missing | Copy `configs/study_config.synthetic.json` to the project root. |
| AI tab reports unavailable | Leave it disabled, or set the API key in the current shell. |
| Browser cannot connect | Confirm the Uvicorn process is running and use the matching port: 8765 for `demo/run_demo.py`, 8000 for the normal source command. |
| Docker cannot write data | Recreate the demo volume with `docker compose -f compose.demo.yml down -v`, then prepare it again. |
| Release check fails | Remove or replace every reported item; do not weaken the scanner to hide it. |
