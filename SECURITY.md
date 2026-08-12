# Security Policy for the Portfolio Demonstration

## Supported use

This is a local, single-user demonstration with fictional inputs. It is not supported as an internet-facing service or for confidential, clinical, or regulated data.

## Reporting a vulnerability

Do not open a public issue containing an exploit, document, credential, or personal information. Report privately to the repository maintainer through the private contact method associated with the repository. If no private method is published, do not send sensitive material; provide only a request for a secure reporting channel.

## Known security boundaries

- No authentication, authorization, tenancy, or role separation
- No application-level encryption for SQLite or uploaded documents
- No guarantee that uploaded files are benign
- Optional transmission of document text to an external model provider
- Local state-changing endpoints use loopback Host/Origin checks but have no user authentication or per-session CSRF token
- Third-party PDF, spreadsheet, archive, and document parsers handling untrusted input

Uploaded and imported files are bounded by compressed size, entry count, expanded size, compression ratio, supported container type, and (for PDFs) page count. Project bundles also use a closed, versioned row schema with foreign-key and embedded-JSON validation. These checks reduce accidental and commodity denial-of-service inputs; they do not make the third-party parsers an isolation boundary. Parsing still occurs in the application process. Treat files from unknown senders as untrusted and do not use this demo to inspect them.

Deleting a project removes its database rows, non-foreign-key audit/review history, and upload directory from the live application. It is a logical deletion, not secure media erasure: SQLite WAL/free pages, filesystem snapshots, backups, exported bundles, logs, and data already sent to an external model provider are outside that guarantee.

The compose file mitigates accidental network exposure by binding to loopback, dropping Linux capabilities, and using a read-only root filesystem. Those controls do not turn the application into a production service.

## Required checks before any authorized release

- Verify vendored PDF.js remains on a reviewed version that includes the fix for [Mozilla advisory 2024-22](https://www.mozilla.org/security/advisories/mfsa2024-22/), and retain the dynamic-evaluation restriction and Content Security Policy.
- Run a dedicated secret scan over current files and full Git history.
- Run dependency, container, and static-analysis scans and review every exception. CI fails on fixable high/critical container findings; advisories without an upstream fix remain visible for review and must be reassessed when the pinned base image changes.
- Verify the existing upload, aggregate-project, request, archive-entry, decompression, and compression-ratio limits against the intended deployment.
- Fuzz or adversarially test PDF, spreadsheet, and project-import boundaries.
- Verify path traversal protections and output encoding.
- Keep the local service on loopback unless a separate authenticated deployment has completed threat modeling and security review.
- Confirm that logs, generated artifacts, and screenshots contain fictional data only.

## Release checks

`python public_release.py --check` audits the candidate source contents. The staged history form audits the exact Git index and all reachable commits. These checks reduce accidental disclosure but do not replace security review.
