# Third-Party Components

This inventory is provided for review and is not legal advice. Exact resolved versions
must be recorded for each authorized release. The generated `sbom-lite.json` and
`PUBLIC_MANIFEST.sha256` supplement this list but are not a formal SPDX or CycloneDX SBOM.

## Vendored browser component

- PDF.js — Apache License 2.0. Its full license header is preserved in
  `static/vendor/PDFJS_LICENSE.txt`; the vendored modules are
  `static/vendor/pdf.mjs` and `static/vendor/pdf.worker.mjs`. The release check
  records the detected version and rejects a known-unsafe legacy baseline.

## Direct Python dependencies

| Component | Declared license |
|---|---|
| FastAPI | MIT |
| Uvicorn | BSD-3-Clause |
| python-multipart | Apache-2.0 |
| pypdf | BSD-3-Clause |
| pdfplumber | MIT |
| pypdfium2 and bundled PDFium | BSD-3-Clause, Apache-2.0, and dependency licenses |
| openpyxl | MIT |
| PyYAML | MIT |
| Anthropic Python SDK | MIT |
| truststore | MIT |
| ReportLab, demo generation only | BSD-style license |

Transitive dependencies have their own terms and must be included in the release
inventory. Do not publish a bundled Python runtime from this working directory; build
platform artifacts through a reviewed, reproducible release process that retains all
required notices and source offers.

The project-level `LICENSE` currently grants no rights. Third-party licenses apply only
to their respective components and do not license the surrounding application.
