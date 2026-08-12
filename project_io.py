"""Project export / import for the LOCAL edition — share one fully-reviewed project across a team as a portable ``*.zip`` bundle.

A bundle is a ZIP containing:

* ``project.json`` — a manifest of every DB row scoped to the project (all 9 tables), plus a small header (``format`` / ``version``). * ``files/<orig_doc_id>/<filename>`` — each document's supported source file.

Import re-inserts every row under a **new** autoincrement project id, remapping all foreign keys AND non-FK logical references (``finding.run_id``, ``comment.finding_id``, ``comment.parent_id``, ``audit_log.entity_id``) via old→new id maps. The whole import runs in ONE transaction on ONE connection with a single commit, so a mid-import failure leaves no half-project on disk or in the database.

LOCAL edition only — the server edition manages projects centrally and does not ship this module. It intentionally does NOT touch ``db.py`` (uses the generic ``db`` helpers), so ``db.py`` stays byte-identical across editions."""

from __future__ import annotations

import io
import json
import math
import os
import re
import shutil
import stat
import zipfile

import db
import pdftools

FORMAT = "tlf_project_bundle"
VERSION = 1

# Project-scoped tables in FK-safe INSERT order (a parent is always inserted before any table that references it). ``audit_log`` / ``review_log`` have no FK to project — they are append-only logs keyed by a plain ``project_id`` int.
_TABLES = ["project", "ai_run", "document", "output", "finding",
           "comment", "annotation", "audit_log", "review_log"]


def _positive_env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return value


# Import guards for the local public demo. These are checked from ZIP metadata before any member is decompressed or any project row/file is written.
_MAX_ENTRIES = _positive_env_int("TLF_ZIP_MAX_ENTRIES", 5000)
_MAX_ENTRY_UNCOMPRESSED = _positive_env_int("TLF_ZIP_MAX_ENTRY_BYTES", 128 * 1024 * 1024)
_MAX_TOTAL_UNCOMPRESSED = _positive_env_int("TLF_ZIP_MAX_TOTAL_BYTES", 256 * 1024 * 1024)
_MAX_COMPRESSION_RATIO = _positive_env_int("TLF_ZIP_MAX_COMPRESSION_RATIO", 100)
_RATIO_CHECK_MIN_BYTES = 1024 * 1024
_MAX_MANIFEST_BYTES = _positive_env_int("TLF_ZIP_MAX_MANIFEST_BYTES", 16 * 1024 * 1024)
_MAX_MANIFEST_ROWS = _positive_env_int("TLF_ZIP_MAX_ROWS", 50_000)
_MAX_TEXT_CHARS = _positive_env_int("TLF_ZIP_MAX_TEXT_CHARS", 1_000_000)
_MAX_ANNOTATION_JSON_CHARS = _positive_env_int("TLF_MAX_ANNOTATION_JSON_CHARS", 512 * 1024)
_MAX_PDF_PAGES = _positive_env_int("TLF_MAX_PDF_PAGES", 2500)
_MAX_OFFICE_ENTRIES = _positive_env_int("TLF_OFFICE_MAX_ENTRIES", 2000)
_MAX_OFFICE_ENTRY_BYTES = _positive_env_int("TLF_OFFICE_MAX_ENTRY_BYTES", 32 * 1024 * 1024)
_MAX_OFFICE_TOTAL_BYTES = _positive_env_int("TLF_OFFICE_MAX_TOTAL_BYTES", 128 * 1024 * 1024)

_DOCUMENT_ROLES = frozenset({"delivery", "prior", "toc", "sap", "protocol", "spp"})
_OUTPUT_STATUSES = frozenset({
    "Not Reviewed", "In Progress", "Manually approved", "Auto-approved",
    "Needs Revision", "Approved", "Rejected",
})
_FINDING_STATES = frozenset({"pending", "posted", "rejected", "resolved"})
_ANNOTATION_KINDS = frozenset({"highlight", "rect", "freehand"})
_DOCUMENT_EXTENSIONS = frozenset({".pdf", ".docx", ".xlsx"})

# Version 1 is an exact, closed schema. Unknown fields cannot become database columns by accident after a future migration, and malformed compound values cannot reach sqlite's binding layer as a 500 response.
_ROW_FIELDS = {
    "project": {"id", "compound", "study", "name", "edition_label", "created_at"},
    "ai_run": {"id", "project_id", "kind", "started_at", "finished_at", "summary_json"},
    "document": {"id", "project_id", "role", "filename", "n_pages", "edition"},
    "output": {
        "id", "project_id", "document_id", "seq", "output_type", "number", "label", "title",
        "page_start", "page_end", "status", "extraction_json", "content_hash", "src_hash", "judge_key",
    },
    "finding": {
        "id", "project_id", "output_id", "run_id", "check_id", "severity", "message", "risk",
        "subjects", "numbers", "page", "printed_page", "pages_total", "section", "row_kind",
        "signature", "state", "badge", "affected", "phase",
    },
    "comment": {
        "id", "project_id", "output_id", "title", "body", "source", "finding_id", "resolved",
        "created_at", "author", "parent_id", "num",
    },
    "annotation": {"id", "output_id", "kind", "page", "geom_json", "note", "created_at"},
    "audit_log": {"id", "ts", "actor", "action", "project_id", "entity", "entity_id", "detail"},
    "review_log": {
        "id", "ts", "reviewer", "action", "project_id", "output_label", "check_id",
        "checklist_item", "risk", "message", "numbers", "subjects", "page", "printed_page",
        "section", "comment_text", "finding_signature",
    },
}
_TEXT_FIELDS = {
    "project": {"compound", "study", "name", "edition_label", "created_at"},
    "ai_run": {"kind", "started_at", "finished_at", "summary_json"},
    "document": {"role", "filename", "edition"},
    "output": {
        "output_type", "number", "label", "title", "status", "extraction_json",
        "content_hash", "src_hash", "judge_key",
    },
    "finding": {
        "check_id", "severity", "message", "risk", "subjects", "numbers", "section",
        "row_kind", "signature", "state", "badge", "affected", "phase",
    },
    "comment": {"title", "body", "source", "created_at", "author"},
    "annotation": {"kind", "geom_json", "note", "created_at"},
    "audit_log": {"ts", "actor", "action", "entity", "detail"},
    "review_log": {
        "ts", "reviewer", "action", "output_label", "check_id", "checklist_item", "risk",
        "message", "numbers", "subjects", "section", "comment_text", "finding_signature",
    },
}


def _reject_json_constant(value: str):
    raise ValueError(f"non-finite JSON number {value} is not permitted")


def _strict_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r} is not permitted")
        result[key] = value
    return result


def _strict_json_loads(value: str):
    return json.loads(
        value,
        parse_constant=_reject_json_constant,
        object_pairs_hook=_strict_json_object,
    )


def _positive_id(value, label: str, *, nullable: bool = False) -> int | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _nonnegative_int(value, label: str, *, nullable: bool = False) -> int | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _json_value(value, label: str, expected: type):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be JSON text")
    try:
        parsed = _strict_json_loads(value)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"{label} is not valid JSON") from exc
    if not isinstance(parsed, expected):
        raise ValueError(f"{label} has the wrong JSON shape")
    return parsed


def validate_annotation_payload(kind, page, geom_json, page_limit: int | None = None) -> str:
    """Validate and canonicalize one annotation accepted from an API or bundle."""
    if kind not in _ANNOTATION_KINDS:
        raise ValueError("annotation kind is not supported")
    page = _positive_id(page, "annotation page")
    if page_limit is not None and page > page_limit:
        raise ValueError("annotation page is outside its output")
    if not isinstance(geom_json, str) or len(geom_json) > _MAX_ANNOTATION_JSON_CHARS:
        raise ValueError("annotation geometry exceeds the configured limit")
    geom = _json_value(geom_json, "annotation geometry", dict)
    if geom is None:
        raise ValueError("annotation geometry must be a JSON object")
    allowed = {"color", "pts"} if kind == "freehand" else {"color", "x", "y", "w", "h"}
    if set(geom) - allowed:
        raise ValueError("annotation geometry contains unknown fields")
    color = geom.get("color", "#ffd54a")
    if not isinstance(color, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
        raise ValueError("annotation color must be a six-digit hex color")

    def coordinate(value, label: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"annotation {label} must be numeric")
        number = float(value)
        if not math.isfinite(number) or not 0 <= number <= 1:
            raise ValueError(f"annotation {label} must be between 0 and 1")
        return number

    if kind == "freehand":
        points = geom.get("pts")
        if not isinstance(points, list) or not 2 <= len(points) <= 5000:
            raise ValueError("freehand annotation must contain 2 to 5000 points")
        clean_points = []
        for index, point in enumerate(points):
            if not isinstance(point, list) or len(point) != 2:
                raise ValueError(f"annotation point {index} must be an [x, y] pair")
            clean_points.append([
                coordinate(point[0], f"point {index} x"),
                coordinate(point[1], f"point {index} y"),
            ])
        clean = {"pts": clean_points, "color": color}
    else:
        x = coordinate(geom.get("x"), "x")
        y = coordinate(geom.get("y"), "y")
        width = coordinate(geom.get("w"), "width")
        height = coordinate(geom.get("h"), "height")
        if width <= 0 or height <= 0 or x + width > 1.000001 or y + height > 1.000001:
            raise ValueError("annotation rectangle must have positive dimensions within the page")
        clean = {"x": x, "y": y, "w": width, "h": height, "color": color}
    return json.dumps(clean, separators=(",", ":"), ensure_ascii=True)


def _validate_office_container(path: str, extension: str) -> None:
    required = {
        ".xlsx": {"[Content_Types].xml", "xl/workbook.xml"},
        ".docx": {"[Content_Types].xml", "word/document.xml"},
    }[extension]
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(infos) > _MAX_OFFICE_ENTRIES:
                raise ValueError("Office document contains too many entries")
            if len(names) != len(set(names)):
                raise ValueError("Office document contains duplicate entries")
            if any(info.flag_bits & 0x1 for info in infos):
                raise ValueError("encrypted Office documents are not supported")
            if any(info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED} for info in infos):
                raise ValueError("Office document uses an unsupported compression method")
            if any(info.file_size > _MAX_OFFICE_ENTRY_BYTES for info in infos):
                raise ValueError("Office document entry is too large")
            if sum(info.file_size for info in infos) > _MAX_OFFICE_TOTAL_BYTES:
                raise ValueError("Office document expands beyond the configured limit")
            for info in infos:
                if info.file_size >= _RATIO_CHECK_MIN_BYTES:
                    ratio = info.file_size / max(1, info.compress_size)
                    if ratio > _MAX_COMPRESSION_RATIO:
                        raise ValueError("Office document has an unsafe compression ratio")
            if not required <= set(names):
                raise ValueError(f"file is not a valid {extension} container")
            if archive.testzip() is not None:
                raise ValueError("Office document failed its CRC check")
    except zipfile.BadZipFile as exc:
        raise ValueError(f"file is not a readable {extension} container") from exc


def validate_document_file(path: str, filename: str) -> int:
    """Validate an uploaded/imported supported document and return its page count."""
    extension = os.path.splitext(filename)[1].lower()
    if extension not in _DOCUMENT_EXTENSIONS:
        raise ValueError("document type is not supported")
    if extension == ".pdf":
        return pdftools.validate_pdf(path, _MAX_PDF_PAGES)
    _validate_office_container(path, extension)
    return 0


def _validate_manifest(manifest: object) -> dict[str, list[dict]]:
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise ValueError("unrecognized bundle format")
    expected_header = {"format", "version", "exported_at", "app", "project_label", "tables"}
    if set(manifest) != expected_header:
        raise ValueError("bundle manifest header does not match the versioned schema")
    if manifest.get("version") != VERSION:
        raise ValueError(f"unsupported bundle version; expected {VERSION}")
    if manifest.get("app") != "tlf_platform" or not isinstance(manifest.get("exported_at"), str):
        raise ValueError("bundle manifest has invalid application metadata")
    raw = manifest.get("tables")
    if not isinstance(raw, dict):
        raise ValueError("bundle manifest has no tables")
    unknown_tables = set(raw) - set(_TABLES)
    if unknown_tables:
        raise ValueError("bundle manifest contains unknown tables")

    tables: dict[str, list[dict]] = {}
    n_rows = 0
    for table in _TABLES:
        rows = raw.get(table, [])
        if not isinstance(rows, list):
            raise ValueError(f"bundle table {table} must be a list")
        n_rows += len(rows)
        if n_rows > _MAX_MANIFEST_ROWS:
            raise ValueError("bundle manifest contains too many rows")
        seen: set[int] = set()
        clean_rows: list[dict] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ValueError(f"bundle table {table} row {index} must be an object")
            fields = set(row)
            if fields != _ROW_FIELDS[table]:
                raise ValueError(f"bundle table {table} row {index} does not match the versioned schema")
            row_id = _positive_id(row.get("id"), f"{table} row id")
            if row_id in seen:
                raise ValueError(f"bundle table {table} contains duplicate row ids")
            seen.add(row_id)
            for key, value in row.items():
                if isinstance(value, (dict, list)) or (isinstance(value, str) and len(value) > _MAX_TEXT_CHARS):
                    raise ValueError(f"bundle field {table}.{key} has an invalid value")
                if key in _TEXT_FIELDS[table] and value is not None and not isinstance(value, str):
                    raise ValueError(f"bundle field {table}.{key} must be text or null")
            clean_rows.append(row)
        tables[table] = clean_rows

    if len(tables["project"]) != 1:
        raise ValueError("bundle must contain exactly one project")
    label = manifest.get("project_label")
    if label is not None and (not isinstance(label, str) or len(label) > _MAX_TEXT_CHARS):
        raise ValueError("bundle project_label must be bounded text")
    project_id = tables["project"][0]["id"]
    for table in ("ai_run", "document", "output", "finding", "comment", "audit_log", "review_log"):
        for row in tables[table]:
            if _positive_id(row.get("project_id"), f"{table}.project_id") != project_id:
                raise ValueError(f"bundle table {table} contains a cross-project row")

    ids = {table: {row["id"] for row in rows} for table, rows in tables.items()}
    documents = {row["id"]: row for row in tables["document"]}
    outputs = {row["id"]: row for row in tables["output"]}
    for row in tables["document"]:
        if row.get("role") not in _DOCUMENT_ROLES:
            raise ValueError("bundle contains an invalid document role")
        _nonnegative_int(row.get("n_pages", 0), "document.n_pages")
        filename = row.get("filename")
        if not isinstance(filename, str) or not filename.strip():
            raise ValueError("bundle document filename must be non-empty text")
        if os.path.splitext(filename)[1].lower() not in _DOCUMENT_EXTENSIONS:
            raise ValueError("bundle document type is not supported")
        if not filename.lower().endswith(".pdf") and row.get("n_pages") != 0:
            raise ValueError("bundle Office document must have n_pages=0")
    for row in tables["ai_run"]:
        _json_value(row.get("summary_json"), "ai_run.summary_json", dict)
    for row in tables["output"]:
        doc_id = _positive_id(row.get("document_id"), "output.document_id")
        if doc_id not in ids["document"]:
            raise ValueError("bundle output references an unknown document")
        if documents[doc_id].get("project_id") != project_id:
            raise ValueError("bundle output references another project")
        start = _positive_id(row.get("page_start"), "output.page_start")
        end = _positive_id(row.get("page_end"), "output.page_end")
        if end < start:
            raise ValueError("bundle output has an inverted page range")
        n_pages = documents[doc_id].get("n_pages") or 0
        if n_pages and end > n_pages:
            raise ValueError("bundle output page range exceeds its document")
        _nonnegative_int(row.get("seq", 0), "output.seq")
        if row.get("status", "Not Reviewed") not in _OUTPUT_STATUSES:
            raise ValueError("bundle contains an invalid output status")
        _json_value(row.get("extraction_json"), "output.extraction_json", dict)
    for row in tables["finding"]:
        output_id = _positive_id(row.get("output_id"), "finding.output_id", nullable=True)
        run_id = _positive_id(row.get("run_id"), "finding.run_id", nullable=True)
        if output_id is not None and output_id not in ids["output"]:
            raise ValueError("bundle finding references an unknown output")
        if run_id is not None and run_id not in ids["ai_run"]:
            raise ValueError("bundle finding references an unknown AI run")
        if row.get("state", "pending") not in _FINDING_STATES:
            raise ValueError("bundle contains an invalid finding state")
        for field in ("page", "printed_page", "pages_total"):
            _positive_id(row.get(field), f"finding.{field}", nullable=True)
        for field in ("subjects", "numbers", "affected"):
            _json_value(row.get(field), f"finding.{field}", list)
    for row in tables["comment"]:
        output_id = _positive_id(row.get("output_id"), "comment.output_id", nullable=True)
        finding_id = _positive_id(row.get("finding_id"), "comment.finding_id", nullable=True)
        parent_id = _positive_id(row.get("parent_id"), "comment.parent_id", nullable=True)
        if output_id is not None and output_id not in ids["output"]:
            raise ValueError("bundle comment references an unknown output")
        if finding_id is not None and finding_id not in ids["finding"]:
            raise ValueError("bundle comment references an unknown finding")
        if parent_id is not None and parent_id not in ids["comment"]:
            raise ValueError("bundle comment references an unknown parent")
        if row.get("resolved", 0) not in (0, 1):
            raise ValueError("bundle comment has an invalid resolved flag")
        _positive_id(row.get("num"), "comment.num", nullable=True)
    for row in tables["annotation"]:
        output_id = _positive_id(row.get("output_id"), "annotation.output_id")
        if output_id not in ids["output"]:
            raise ValueError("bundle annotation references an unknown output")
        output = outputs[output_id]
        page_limit = output["page_end"] - output["page_start"] + 1
        row["geom_json"] = validate_annotation_payload(
            row.get("kind"), row.get("page"), row.get("geom_json"), page_limit,
        )
    for row in tables["review_log"]:
        for field in ("page", "printed_page"):
            _positive_id(row.get(field), f"review_log.{field}", nullable=True)
        for field in ("numbers", "subjects"):
            _json_value(row.get(field), f"review_log.{field}", list)
    return tables


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #

def export_bundle(pid: int) -> tuple[bytes, list[str]]:
    """Build an in-memory ``*.zip`` bundle for one project.

Returns ``(zip_bytes, warnings)``. JSON columns are carried as DB text in the manifest; import parses their expected shape before writing and canonicalizes annotation geometry."""
    proj = db.one("SELECT * FROM project WHERE id=?", (pid,))
    if not proj:
        raise ValueError("project not found")

    documents = db.query("SELECT * FROM document WHERE project_id=? ORDER BY id", (pid,))
    document_paths = {row["id"]: row.get("path") or "" for row in documents}
    tables = {
        "project": [proj],
        "ai_run": db.query("SELECT * FROM ai_run WHERE project_id=? ORDER BY id", (pid,)),
        # Absolute workstation paths are runtime implementation details. Excluding them prevents a shared bundle from disclosing a username or directory layout; import reconstructs every path under its own uploads directory.
        "document": [{k: v for k, v in row.items() if k != "path"} for row in documents],
        "output": db.query("SELECT * FROM output WHERE project_id=? ORDER BY id", (pid,)),
        "finding": db.query("SELECT * FROM finding WHERE project_id=? ORDER BY id", (pid,)),
        "comment": db.query("SELECT * FROM comment WHERE project_id=? ORDER BY id", (pid,)),
        # annotation has no project_id — reach it through its owning output.
        "annotation": db.query(
            "SELECT * FROM annotation WHERE output_id IN "
            "(SELECT id FROM output WHERE project_id=?) ORDER BY id", (pid,)),
        "audit_log": db.query("SELECT * FROM audit_log WHERE project_id=? ORDER BY id", (pid,)),
        "review_log": db.query("SELECT * FROM review_log WHERE project_id=? ORDER BY id", (pid,)),
    }

    label = " / ".join(x for x in (proj.get("compound"), proj.get("study"),
                                   proj.get("name")) if x)
    manifest = {
        "format": FORMAT,
        "version": VERSION,
        "exported_at": db.now_iso(),
        "app": "tlf_platform",
        "project_label": label,
        "tables": tables,
    }

    warnings: list[str] = []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("project.json", json.dumps(manifest, ensure_ascii=False))
        for d in tables["document"]:
            path = document_paths.get(d["id"], "")
            if path and os.path.isfile(path):
                # Archive key is built ONLY from the trusted doc id + basename; never from any client-supplied path.
                z.write(path, f"files/{d['id']}/{os.path.basename(path)}")
            else:
                warnings.append(
                    f"File missing on disk for document {d['id']} "
                    f"({d.get('filename')!r}); exported without the file.")
    return buf.getvalue(), warnings


# --------------------------------------------------------------------------- #
# Import
# --------------------------------------------------------------------------- #

def _live_columns(conn, table: str) -> set:
    """Columns that currently exist in the DB for ``table`` (so migration-added columns pass through and version-skew never crashes the INSERT)."""
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _clean(row: dict, live: set) -> dict:
    """A row minus its primary key, keeping only columns that still exist."""
    return {k: v for k, v in row.items() if k != "id" and k in live}


def _insert(conn, table: str, cols: dict) -> int:
    """Non-committing INSERT (unlike db.insert) so the whole import is one txn."""
    keys = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    cur = conn.execute(f"INSERT INTO {table} ({keys}) VALUES ({marks})",
                       tuple(cols.values()))
    return cur.lastrowid


def import_bundle(zip_bytes: bytes, uploads_dir: str, safe_filename,
                  mode: str = "ask") -> dict:
    """Import a ``*.zip`` bundle as a NEW project.

``mode``: ``"ask"`` (default) returns ``{"conflict": True, ...}`` without writing if a project with the same compound/study/name already exists; ``"new"`` always imports as a fresh copy; ``"replace"`` first deletes every matching project (and its files) then imports.

Returns ``{"id", "name", "label", "counts", "warnings", "replaced"}`` on a successful import, or ``{"conflict": True, "existing", "label"}`` when asked. Raises ``ValueError`` on a malformed/hostile bundle (the endpoint maps that to HTTP 400)."""
    if mode not in {"ask", "new", "replace"}:
        raise ValueError("import mode must be 'ask', 'new', or 'replace'")

    # 1. Open + validate the manifest.
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        raise ValueError("not a valid .zip file")

    with zf:
        infos = zf.infolist()
        if len(infos) > _MAX_ENTRIES:
            raise ValueError("bundle has too many entries")
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise ValueError("bundle contains duplicate member names")
        if any(info.flag_bits & 0x1 for info in infos):
            raise ValueError("encrypted bundle entries are not supported")
        if any(info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED} for info in infos):
            raise ValueError("bundle uses an unsupported compression method")
        if any(stat.S_ISLNK(info.external_attr >> 16) for info in infos):
            raise ValueError("bundle contains a symbolic-link entry")
        if any(info.file_size > _MAX_ENTRY_UNCOMPRESSED for info in infos):
            raise ValueError("bundle entry is too large")
        if sum(info.file_size for info in infos) > _MAX_TOTAL_UNCOMPRESSED:
            raise ValueError("bundle is too large")
        for info in infos:
            if info.file_size < _RATIO_CHECK_MIN_BYTES:
                continue
            ratio = info.file_size / max(1, info.compress_size)
            if ratio > _MAX_COMPRESSION_RATIO:
                raise ValueError("bundle entry has an unsafe compression ratio")
        manifest_info = next((info for info in infos if info.filename == "project.json"), None)
        if manifest_info is None:
            raise ValueError("missing project.json — not a project bundle")
        if manifest_info.file_size > _MAX_MANIFEST_BYTES:
            raise ValueError("project.json is too large")
        try:
            manifest = _strict_json_loads(zf.read("project.json").decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            raise ValueError("project.json is not valid JSON")
        tables = _validate_manifest(manifest)
        proj = tables["project"][0]

        # Index document members once by their doc-id prefix files/<orig_id>/... . The archive member name is used ONLY as a lookup key into the zip; it is never used as a filesystem path (traversal-safe).
        members_by_docid: dict[str, str] = {}
        known_docids = {str(row["id"]) for row in tables["document"]}
        for info in infos:
            name = info.filename
            if name == "project.json":
                continue
            match = re.fullmatch(r"files/([1-9]\d*)/([^/]+)", name)
            if not match or info.is_dir():
                raise ValueError("bundle contains an unexpected member path")
            doc_id = match.group(1)
            if doc_id not in known_docids:
                raise ValueError("bundle contains a file for an unknown document")
            if doc_id in members_by_docid:
                raise ValueError("bundle contains multiple files for one document")
            members_by_docid[doc_id] = name

        label = manifest.get("project_label") or (proj.get("name") or "project")

        # 2. Collision detection on compound/study/name (IS handles NULLs).
        matches = db.query(
            "SELECT id, name FROM project WHERE compound IS ? AND study IS ? AND name IS ?",
            (proj.get("compound"), proj.get("study"), proj.get("name")))
        if mode == "ask" and matches:
            return {"conflict": True, "existing": matches, "label": label}

        # 3. Import in ONE transaction on ONE connection (single commit).
        conn = db.get()
        new_pid = None
        replaced: list[int] = []
        old_dirs: list[str] = []
        warnings: list[str] = []
        try:
            live = {t: _live_columns(conn, t) for t in _TABLES}

            if mode == "replace" and matches:
                ids = [m["id"] for m in matches]
                qs = ",".join("?" for _ in ids)
                # Cascade removes document/output/comment/finding/annotation/ai_run; the logs have no FK, so clear them explicitly. Defer the file rmtree until AFTER commit (so a failed import can't lose them).
                conn.execute(f"DELETE FROM project WHERE id IN ({qs})", tuple(ids))
                conn.execute(f"DELETE FROM audit_log WHERE project_id IN ({qs})", tuple(ids))
                conn.execute(f"DELETE FROM review_log WHERE project_id IN ({qs})", tuple(ids))
                replaced = ids
                old_dirs = [os.path.join(uploads_dir, str(i)) for i in ids]

            # project
            new_pid = _insert(conn, "project", _clean(proj, live["project"]))
            proj_dir = os.path.join(uploads_dir, str(new_pid))
            os.makedirs(proj_dir, exist_ok=True)

            # ai_run
            runmap: dict = {}
            for r in tables["ai_run"]:
                c = _clean(r, live["ai_run"])
                c["project_id"] = new_pid
                runmap[r["id"]] = _insert(conn, "ai_run", c)

            # document (+ write each supported file next to the new project)
            docmap: dict = {}
            used_filenames: set[str] = set()
            for r in tables["document"]:
                c = _clean(r, live["document"])
                c["project_id"] = new_pid
                original_name = safe_filename(r.get("filename") or "upload.pdf")
                filename = original_name
                stem, extension = os.path.splitext(original_name)
                collision = 0
                while filename.casefold() in used_filenames:
                    collision += 1
                    suffix = f"_{r['id']}" if collision == 1 else f"_{r['id']}_{collision}"
                    filename = f"{stem}{suffix}{extension}"
                used_filenames.add(filename.casefold())
                c["filename"] = filename
                dest = os.path.join(proj_dir, filename)
                member = members_by_docid.get(str(r["id"]))
                if member is not None:
                    try:
                        with zf.open(member) as src, open(dest, "wb") as fh:
                            shutil.copyfileobj(src, fh)
                    except (zipfile.BadZipFile, RuntimeError, EOFError) as exc:
                        raise ValueError("bundle document failed its archive integrity check") from exc
                    actual_pages = validate_document_file(dest, filename)
                    declared_pages = r.get("n_pages") or 0
                    if filename.lower().endswith(".pdf") and actual_pages != declared_pages:
                        raise ValueError("bundle PDF page count does not match its manifest")
                else:
                    warnings.append(
                        f"File for document {r['id']} ({r.get('filename')!r}) not in "
                        f"bundle; row kept without a file.")
                c["path"] = dest      # absolute on-disk path, like create_project
                docmap[r["id"]] = _insert(conn, "document", c)

            # output
            outmap: dict = {}
            for r in tables["output"]:
                c = _clean(r, live["output"])
                c["project_id"] = new_pid
                c["document_id"] = docmap.get(r.get("document_id"))
                outmap[r["id"]] = _insert(conn, "output", c)

            # finding — .get() everywhere: output_id/run_id are nullable (cross-output XOUT findings have output_id=None).
            findmap: dict = {}
            for r in tables["finding"]:
                c = _clean(r, live["finding"])
                c["project_id"] = new_pid
                c["output_id"] = outmap.get(r.get("output_id"))
                if "run_id" in c:
                    c["run_id"] = runmap.get(r.get("run_id"))
                findmap[r["id"]] = _insert(conn, "finding", c)

            # comment — two passes for self-referential parent_id (order-independent)
            commap: dict = {}
            for r in tables["comment"]:
                c = _clean(r, live["comment"])
                c["project_id"] = new_pid
                c["output_id"] = outmap.get(r.get("output_id"))
                if "finding_id" in c:
                    c["finding_id"] = findmap.get(r.get("finding_id"))
                if "parent_id" in c:
                    c["parent_id"] = None
                commap[r["id"]] = _insert(conn, "comment", c)
            if "parent_id" in live["comment"]:
                for r in tables["comment"]:
                    old_parent = r.get("parent_id")
                    if old_parent:
                        conn.execute("UPDATE comment SET parent_id=? WHERE id=?",
                                     (commap.get(old_parent), commap[r["id"]]))

            # annotation (no project_id column; keyed to its output)
            for r in tables["annotation"]:
                c = _clean(r, live["annotation"])
                c["output_id"] = outmap.get(r.get("output_id"))
                _insert(conn, "annotation", c)

            # audit_log — remap entity_id by entity kind; a target that no longer exists (append-only log of a since-deleted row) becomes NULL. Never re-insert a raw old id (it would point at an unrelated project's row).
            entity_maps = {"output": outmap, "comment": commap, "finding": findmap}
            for r in tables["audit_log"]:
                c = _clean(r, live["audit_log"])
                c["project_id"] = new_pid
                ent = r.get("entity")
                if ent == "project":
                    c["entity_id"] = new_pid
                else:
                    m = entity_maps.get(ent)
                    c["entity_id"] = m.get(r.get("entity_id")) if m is not None else None
                _insert(conn, "audit_log", c)

            # review_log — only project_id is an id; output_label / finding_signature are TEXT and stay as-is.
            for r in tables["review_log"]:
                c = _clean(r, live["review_log"])
                c["project_id"] = new_pid
                _insert(conn, "review_log", c)

            conn.commit()
        except Exception:
            conn.rollback()
            if new_pid is not None:
                shutil.rmtree(os.path.join(uploads_dir, str(new_pid)), ignore_errors=True)
            raise

        # Post-commit: now safe to drop the replaced projects' upload folders.
        for d in old_dirs:
            shutil.rmtree(d, ignore_errors=True)

        counts = {t: len(tables[t]) for t in _TABLES}
        return {"id": new_pid, "name": proj.get("name"), "label": label,
                "counts": counts, "warnings": warnings, "replaced": replaced}
