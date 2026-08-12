# Privacy and Data Handling

## Demonstration policy

Use only the generated fictional documents. Do not upload protected health information, personal data, confidential business information, proprietary study documents, or regulated records to this portfolio build.

## Local processing and storage

The FastAPI server runs locally by default. Uploaded documents, extracted text, findings, comments, annotations, and audit-oriented events are stored under `data/` in ordinary files and SQLite. The application does not encrypt this directory and does not provide authentication or per-user isolation.

`data/` is excluded from Git, Docker build context, and public source packages, but an ignore rule is not a backup, access-control, or deletion policy. Users remain responsible for filesystem permissions and secure disposal.

## External AI processing

When AI review or chat is enabled, the application sends relevant content to the configured model endpoint. Depending on the action, that content can include:

- extracted TLF page text and table labels;
- structured table values and candidate findings;
- output and project metadata;
- user questions; and
- portions of optional analysis-plan or protocol reference documents.

The endpoint, retention terms, geographic processing, training policy, and access controls depend on the user's provider and account. Review those terms independently. The application does not anonymize free text before transmission.

## Secrets

Supply API keys through the current shell or an approved secret manager. Never store them in source, `.env` files committed to Git, compose files, screenshots, logs, or exported project bundles. The public release scanner looks for common key formats, but it is not a substitute for a dedicated secret scanner and repository-history audit.

## Telemetry

The application records local request information, model identifiers, token counts, review activity, and errors. Review logs before sharing them; filenames and project metadata may be sensitive even when document contents are absent.

## Deletion

Removing a project through the app or deleting local `data/` does not control copies already exported, backed up, synchronized, logged, or sent to an external provider. This demonstration makes no claim of verified erasure.
