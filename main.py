"""TLF Review Platform — FastAPI backend.

Single-user (no auth). Serves the SPA in static/ and a JSON/binary API for projects, outputs, the clipped-PDF viewer, comments, annotations, AI review, and exports."""

from __future__ import annotations

from contextlib import asynccontextmanager
import logging
import os
import re
import shutil
from urllib.parse import urlsplit

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import Response, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

logger = logging.getLogger("tlf")


def _positive_env_int(name: str, default: int) -> int:
    """Read one positive integer limit and fail loudly on unsafe configuration."""
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


# Conservative defaults for the local public demonstration. Operators who have reviewed a larger trusted workload can raise them explicitly in the environment.
UPLOAD_CHUNK_BYTES = 1024 * 1024
MAX_DOCUMENT_BYTES = _positive_env_int("TLF_MAX_DOCUMENT_BYTES", 64 * 1024 * 1024)
MAX_PROJECT_UPLOAD_BYTES = _positive_env_int("TLF_MAX_PROJECT_UPLOAD_BYTES", 192 * 1024 * 1024)
MAX_PROJECT_FILES = _positive_env_int("TLF_MAX_PROJECT_FILES", 12)
MAX_BUNDLE_BYTES = _positive_env_int("TLF_MAX_BUNDLE_BYTES", 128 * 1024 * 1024)
MAX_SHEET_BYTES = _positive_env_int("TLF_MAX_SHEET_BYTES", 10 * 1024 * 1024)
MAX_REQUEST_BYTES = _positive_env_int("TLF_MAX_REQUEST_BYTES", 256 * 1024 * 1024)
HUMAN_OUTPUT_STATUSES = frozenset({
    "Not Reviewed",
    "In Progress",
    "Manually approved",
    "Needs Revision",
})
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# SHA-256 hashes of the three intentionally inline bootstrap scripts in static/index.html and static/tutorial.html (the theme script is shared). This keeps inline event handlers disabled even if an output-encoding regression is introduced later. tests/test_frontend_safety.py binds these hashes to the files.
INLINE_SCRIPT_HASHES = (
    "'sha256-ih8H3OtJF4l1wbGHI7OEfLSrSwZVcmrFJlTtYEm9adA='",
    "'sha256-z5tJ6H+wdKdAQqOlmyWzSClocNRR2DHEqK2URy2Aygs='",
    "'sha256-yKsyuYEJUKCHWIOdD4QC6v5GvFefPwNaWIBdFkC6uyE='",
)
TRUSTED_HOSTS = tuple(
    host.strip().lower()
    for host in os.environ.get(
        "TLF_TRUSTED_HOSTS",
        "127.0.0.1,localhost,testserver",
    ).split(",")
    if host.strip()
)
if not TRUSTED_HOSTS:
    raise RuntimeError("TLF_TRUSTED_HOSTS must contain at least one host")
if any(re.search(r"[/:@*\\\s]", host) for host in TRUSTED_HOSTS):
    raise RuntimeError(
        "TLF_TRUSTED_HOSTS entries must be literal hostnames or IPv4 addresses without ports"
    )


def _too_large(label: str, limit: int) -> HTTPException:
    return HTTPException(413, f"{label} exceeds the configured {limit}-byte limit")


async def _read_upload_limited(upload: UploadFile, limit: int, label: str) -> bytes:
    """Read an upload in bounded chunks, rejecting bytes beyond ``limit``."""
    data = bytearray()
    while True:
        chunk = await upload.read(UPLOAD_CHUNK_BYTES)
        if not chunk:
            return bytes(data)
        if len(data) + len(chunk) > limit:
            raise _too_large(label, limit)
        data.extend(chunk)


async def _save_upload_limited(upload: UploadFile, dest: str, limit: int, label: str) -> int:
    """Stream one upload to disk with a hard byte limit and partial-file cleanup."""
    written = 0
    try:
        with open(dest, "wb") as fh:
            while True:
                chunk = await upload.read(UPLOAD_CHUNK_BYTES)
                if not chunk:
                    return written
                if written + len(chunk) > limit:
                    raise _too_large(label, limit)
                fh.write(chunk)
                written += len(chunk)
    except Exception:
        try:
            os.unlink(dest)
        except FileNotFoundError:
            pass
        raise


def safe_filename(name: str) -> str:
    """Strip any path components and unsafe characters so an uploaded filename can never escape the project folder (defends against path-traversal like ../../x)."""
    base = os.path.basename((name or "").replace("\\", "/"))
    base = re.sub(r"[^A-Za-z0-9._ +\-]", "_", base).strip("._ ") or "upload"
    return base[:180]


def _unique_filename(directory: str, requested: str) -> str:
    """Return a sanitized, case-insensitively unique filename in ``directory``."""
    filename = safe_filename(requested)
    occupied = {name.casefold() for name in os.listdir(directory)} if os.path.isdir(directory) else set()
    if filename.casefold() not in occupied:
        return filename
    stem, extension = os.path.splitext(filename)
    counter = 2
    while True:
        candidate = f"{stem}_{counter}{extension}"
        if candidate.casefold() not in occupied:
            return candidate
        counter += 1


def _content_headers(filename: str, disposition: str = "attachment") -> dict[str, str]:
    """Build injection-safe download headers for locally generated content."""
    if disposition not in {"attachment", "inline"}:
        raise ValueError("invalid content disposition")
    filename = safe_filename(filename)
    return {
        "Content-Disposition": f'{disposition}; filename="{filename}"',
        "Cache-Control": "no-store",
    }

import db
import indexer
import pdftools
import export
import runner
import chat
import ai_client
import ai_review
import project_io

BASE = os.path.dirname(__file__)
STATIC = os.path.join(BASE, "static")
UPLOADS = os.path.join(db.DATA_DIR, "uploads")
DEMO_MODE = os.environ.get("TLF_DEMO_MODE") == "1"


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    os.makedirs(UPLOADS, exist_ok=True)
    db.init()
    # Crash recovery: any AI run left unfinished by a restart is marked interrupted so the UI doesn't show a run stuck "in progress" forever.
    n = db.recover_stale_runs()
    if n:
        logger.warning("Marked %d interrupted AI run(s) after restart", n)
    yield


app = FastAPI(title="TLF Review Platform", lifespan=_lifespan)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=list(TRUSTED_HOSTS),
    www_redirect=False,
)


def _same_origin(request: Request) -> bool:
    """Accept absent CLI origins, but reject cross-origin browser mutations."""
    origin = request.headers.get("origin")
    if not origin:
        return request.headers.get("sec-fetch-site", "").lower() != "cross-site"
    try:
        parsed = urlsplit(origin)
        origin_host = (parsed.hostname or "").lower()
        request_host = (request.url.hostname or "").lower()
        origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        request_port = request.url.port or (443 if request.url.scheme == "https" else 80)
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme in {"http", "https"}
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
        and origin_host in TRUSTED_HOSTS
        and origin_host == request_host
        and parsed.scheme == request.url.scheme
        and origin_port == request_port
    )


@app.middleware("http")
async def _log_requests(request: Request, call_next):
    import time
    t0 = time.perf_counter()
    resp = None
    if request.method in UNSAFE_METHODS and not _same_origin(request):
        resp = JSONResponse(
            status_code=403,
            content={"detail": "cross-origin state-changing requests are not allowed"},
        )
    # Reject ordinary oversized requests before Starlette parses/spools multipart data. Per-file streaming limits below remain authoritative when Content-Length is absent or untrustworthy (for example, chunked transfer encoding).
    content_length = request.headers.get("content-length")
    if resp is None and content_length:
        try:
            declared = int(content_length)
        except ValueError:
            declared = -1
        if declared < 0:
            resp = JSONResponse(status_code=400, content={"detail": "invalid Content-Length header"})
        elif declared > MAX_REQUEST_BYTES:
            resp = JSONResponse(
                status_code=413,
                content={"detail": f"request exceeds the configured {MAX_REQUEST_BYTES}-byte limit"},
            )
    if resp is None:
        resp = await call_next(request)
    if request.url.path.startswith("/api/"):
        logger.info("%s %s -> %s %.0fms", request.method, request.url.path,
                    resp.status_code, (time.perf_counter() - t0) * 1000)
    # The app renders uploaded PDFs, so lock the browser to local application resources and forbid plug-ins/framing. Inline style/script is retained only for the tiny pre-paint theme snippet and module bootstrap in index.html.
    resp.headers.setdefault("Content-Security-Policy", "; ".join((
        "default-src 'self'", "base-uri 'none'", "object-src 'none'",
        "frame-ancestors 'none'", "form-action 'self'",
        f"script-src 'self' {' '.join(INLINE_SCRIPT_HASHES)}",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:", "font-src 'self'",
        "connect-src 'self'", "worker-src 'self' blob:",
    )))
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    resp.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    if request.url.path.startswith("/api/"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    """Return a clean JSON error instead of leaking a stack trace to the client; the full traceback is logged server-side."""
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"error": "Internal server error. See server log."})


# --------------------------------------------------------------------------- #
# Projects
# --------------------------------------------------------------------------- #

@app.get("/api/runtime-info")
def runtime_info():
    """Non-secret UI flags. Demo disclosure is intentionally impossible to dismiss."""
    return {
        "demo_mode": DEMO_MODE,
        "external_ai_enabled": bool(ai_client.available()),
        "notice": ("SYNTHETIC DEMO — no real clinical or patient data; "
                   "AI behavior is simulated.") if DEMO_MODE else "",
    }

@app.get("/api/projects")
def list_projects():
    rows = db.query("SELECT * FROM project ORDER BY created_at DESC")
    for r in rows:
        # Only the main (reviewed) document's tables count as outputs; comparison editions are indexed for cross-edition checks but are not review targets.
        current, _ = runner._pick_current_prior(r["id"])
        r["n_outputs"] = db.one(
            "SELECT COUNT(*) c FROM output WHERE project_id=? AND document_id=?",
            (r["id"], current["id"]))["c"] if current else 0
    return rows


@app.post("/api/projects")
async def create_project(
    compound: str = Form(...),
    study: str = Form(""),
    name: str = Form(...),
    edition_label: str = Form(""),
    delivery: list[UploadFile] = File(...),
    main_index: int = Form(-1),
    toc: UploadFile | None = File(None),
    sap: UploadFile | None = File(None),
    protocol: UploadFile | None = File(None),
    spp: UploadFile | None = File(None),
):
    if not delivery or any(not upload.filename for upload in delivery):
        raise HTTPException(400, "at least one delivery PDF is required")
    if main_index < -1 or main_index >= len(delivery):
        raise HTTPException(400, "main_index is outside the delivery file list")
    optional_uploads = [(toc, "toc"), (sap, "sap"), (protocol, "protocol"), (spp, "spp")]
    n_files = len(delivery) + sum(1 for upload, _ in optional_uploads if upload and upload.filename)
    if n_files > MAX_PROJECT_FILES:
        raise HTTPException(413, f"project upload exceeds the configured {MAX_PROJECT_FILES}-file limit")

    pid = db.insert("project", compound=compound, study=study, name=name,
                    edition_label=edition_label, created_at=db.now_iso())
    proj_dir = os.path.join(UPLOADS, str(pid))
    try:
        os.makedirs(proj_dir, exist_ok=True)
    except Exception:
        db.execute("DELETE FROM project WHERE id=?", (pid,))
        raise

    project_bytes = 0

    async def _save(upload: UploadFile, role: str):
        nonlocal project_bytes
        expected_extensions = {
            "delivery": {".pdf"},
            "prior": {".pdf"},
            "toc": {".xlsx"},
            "sap": {".pdf", ".docx"},
            "protocol": {".pdf", ".docx"},
            "spp": {".pdf", ".docx"},
        }[role]
        requested_name = safe_filename(upload.filename)
        if os.path.splitext(requested_name)[1].lower() not in expected_extensions:
            allowed = ", ".join(sorted(expected_extensions))
            raise HTTPException(400, f"{role} document must use one of: {allowed}")
        name = _unique_filename(proj_dir, requested_name)
        dest = os.path.join(proj_dir, name)
        size = await _save_upload_limited(upload, dest, MAX_DOCUMENT_BYTES, "document upload")
        if project_bytes + size > MAX_PROJECT_UPLOAD_BYTES:
            raise _too_large("project upload", MAX_PROJECT_UPLOAD_BYTES)
        project_bytes += size
        is_pdf = dest.lower().endswith(".pdf")
        try:
            n_pages = project_io.validate_document_file(dest, name)
            edition = runner.detect_edition(dest) if (is_pdf and role in ("delivery", "prior")) else ""
        except ValueError as exc:
            raise HTTPException(400, f"Invalid {role} document: {exc}") from exc
        except Exception as exc:
            raise HTTPException(400, f"The {role} document could not be parsed") from exc
        doc_id = db.insert("document", project_id=pid, role=role,
                           filename=name, path=dest,
                           n_pages=n_pages,
                           edition=edition)
        return doc_id, dest

    total_outputs = 0
    try:
        for i, up in enumerate(delivery):
            # The user-marked main document is the edition under review (role='delivery'); any others are kept as comparison editions (role='prior'). With no explicit pick (main_index<0, e.g. a single upload) every delivery doc stays 'delivery' and the reviewer falls back to the highest-edition auto-pick.
            role = "prior" if (main_index >= 0 and i != main_index) else "delivery"
            doc_id, dest = await _save(up, role)
            try:
                indexed = indexer.index_delivery(dest)
            except Exception as exc:
                raise HTTPException(400, "A delivery PDF could not be indexed") from exc
            for o in indexed:   # index all editions: prior outputs feed comparison checks
                db.insert("output", project_id=pid, document_id=doc_id, **o)
                total_outputs += 1
        # Optional documents (TOC workbook + AI reference docs). Stored for reference; indexing is bookmark-driven, so the TOC workbook is not required.
        for up, role in optional_uploads:
            if up is not None and up.filename:
                await _save(up, role)
    except Exception:
        db.execute("DELETE FROM project WHERE id=?", (pid,))
        shutil.rmtree(proj_dir, ignore_errors=True)
        raise

    db.audit("system", "project.create", "project", pid, pid,
             f"{compound}/{study}/{name}; {total_outputs} outputs")

    # Run the deterministic (non-AI) structural checks now, so blank pages / numbering gaps / missing outputs are visible on the TOC page before any AI review is triggered. Guarded so a check failure can never block project creation.
    try:
        n_structural = runner.run_structural_checks(pid)
    except Exception:
        logger.exception("structural checks failed for project %s", pid)
        n_structural = 0
    # Report the main (reviewed) document's table count — comparison editions were indexed too (see loop above) but are not review targets, so they don't count.
    current, _ = runner._pick_current_prior(pid)
    main_outputs = db.one(
        "SELECT COUNT(*) c FROM output WHERE project_id=? AND document_id=?",
        (pid, current["id"]))["c"] if current else total_outputs
    return {"id": pid, "n_outputs": main_outputs, "n_structural": n_structural}


@app.get("/api/projects/{pid}")
def get_project(pid: int):
    proj = db.one("SELECT * FROM project WHERE id=?", (pid,))
    if not proj:
        raise HTTPException(404, "project not found")
    proj["documents"] = db.query("SELECT id, role, filename, n_pages, edition FROM document WHERE project_id=?", (pid,))
    # cov_read / cov_total come from the stored extraction so the TOC can show how much of each output the AI actually read. Pulled with json_extract rather than shipping the whole extraction_json (which is up to ~150 KB per output).
    proj["outputs"] = db.query(
        """SELECT o.id, o.document_id, o.seq, o.output_type, o.number, o.label, o.title,
                  o.page_start, o.page_end, o.status, d.filename AS doc_filename, d.edition,
                  json_extract(o.extraction_json, '$.coverage.pages_read')  AS cov_read,
                  json_extract(o.extraction_json, '$.coverage.pages_total') AS cov_total,
                  (SELECT COUNT(*) FROM comment c WHERE c.output_id=o.id) AS n_comments
           FROM output o LEFT JOIN document d ON d.id=o.document_id
           WHERE o.project_id=? ORDER BY o.document_id, o.seq""", (pid,))
    return proj


@app.delete("/api/projects/{pid}")
def delete_project(pid: int):
    if not db.one("SELECT id FROM project WHERE id=?", (pid,)):
        raise HTTPException(404, "project not found")
    # audit_log/review_log intentionally have no FK so deleted-target history can exist while a project is live. A project deletion is a privacy deletion, however, so remove those rows in the same transaction as the project.
    conn = db.get()
    try:
        conn.execute("DELETE FROM audit_log WHERE project_id=?", (pid,))
        conn.execute("DELETE FROM review_log WHERE project_id=?", (pid,))
        conn.execute("DELETE FROM project WHERE id=?", (pid,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    shutil.rmtree(os.path.join(UPLOADS, str(pid)), ignore_errors=True)
    return {"ok": True}


@app.get("/api/projects/{pid}/export/project.zip")
def export_project(pid: int):
    """Download one project as a portable *.zip (rows + PDFs) to share with a teammate, who imports it via POST /api/projects/import."""
    proj = db.one("SELECT compound, study, name FROM project WHERE id=?", (pid,))
    if not proj:
        raise HTTPException(404, "project not found")
    data, warnings = project_io.export_bundle(pid)
    for w in warnings:
        logger.warning("export project %s: %s", pid, w)
    stem = safe_filename("_".join(x for x in (proj["compound"], proj["study"],
                                              proj["name"]) if x) or f"project_{pid}")
    return Response(data, media_type="application/zip",
                    headers=_content_headers(f"{stem}.zip"))


@app.post("/api/projects/import")
async def import_project(file: UploadFile = File(...), mode: str = Form("ask")):
    """Import a *.zip project bundle as a NEW project. mode='ask' (default) returns a {conflict} flag when a same-compound/study/name project exists so the UI can prompt replace-vs-add; the UI then re-POSTs with mode='replace' or 'new'."""
    data = await _read_upload_limited(file, MAX_BUNDLE_BYTES, "project bundle")
    try:
        res = project_io.import_bundle(data, UPLOADS, safe_filename, mode=mode)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if res.get("conflict"):
        return res
    # Audit only AFTER the import's own commit (db.audit commits on this connection).
    db.audit("system", "project.import", "project", res["id"], res["id"],
             f'{res.get("label")}; ' + ", ".join(f"{k}={v}" for k, v in res["counts"].items()))
    for w in res.get("warnings", []):
        logger.warning("import project %s: %s", res["id"], w)
    return res


# --------------------------------------------------------------------------- #
# Outputs / viewer
# --------------------------------------------------------------------------- #

@app.get("/api/tlf-clip")
def tlf_clip(output_id: int):
    o = db.one("SELECT * FROM output WHERE id=?", (output_id,))
    if not o:
        raise HTTPException(404, "output not found")
    doc = db.one("SELECT * FROM document WHERE id=?", (o["document_id"],))
    pdf = pdftools.clip(doc["path"], o["page_start"], o["page_end"])
    return Response(pdf, media_type="application/pdf",
                    headers=_content_headers(f'{o["label"]}.pdf', "inline"))


@app.post("/api/outputs/{oid}/status")
def set_status(oid: int, status: str = Form(...), actor: str = Form("Reviewer")):
    if status not in HUMAN_OUTPUT_STATUSES:
        raise HTTPException(400, "invalid output status")
    o = db.one("SELECT project_id, status FROM output WHERE id=?", (oid,))
    if not o:
        raise HTTPException(404, "output not found")
    db.execute("UPDATE output SET status=? WHERE id=?", (status, oid))
    db.audit(actor, "status.set", "output", oid, o["project_id"],
             f"{o['status']} → {status}")
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Comments
# --------------------------------------------------------------------------- #

@app.get("/api/projects/{pid}/comments")
def list_comments(pid: int):
    return db.query(
        """SELECT c.*, o.label AS output_label, o.title AS output_title,
                  o.page_start AS page
           FROM comment c LEFT JOIN output o ON o.id=c.output_id
           WHERE c.project_id=? ORDER BY c.created_at DESC""", (pid,))


def _next_comment_num(output_id: int) -> int:
    """The next per-Table comment ID: one past the current max for this output, or 1."""
    row = db.one("SELECT COALESCE(MAX(num), 0) + 1 AS n FROM comment WHERE output_id=?", (output_id,))
    return (row or {}).get("n") or 1


@app.post("/api/outputs/{oid}/comments")
def add_comment(oid: int, body: str = Form(...)):
    o = db.one("SELECT project_id, label FROM output WHERE id=?", (oid,))
    if not o:
        raise HTTPException(404, "output not found")
    num = _next_comment_num(oid)
    cid = db.insert("comment", project_id=o["project_id"], output_id=oid, num=num,
                    title="", body=body, source="manual", parent_id=None,
                    created_at=db.now_iso())
    # Commenting nudges the output into review.
    db.execute("UPDATE output SET status='In Progress' WHERE id=? AND status='Not Reviewed'", (oid,))
    db.audit("Reviewer", "comment.add", "comment", cid, o["project_id"], body[:200])
    # A top-level comment is a human review action worth learning from.
    db.log_comment_action("Reviewer", "comment_add", o["project_id"], o.get("label") or "", body)
    return {"id": cid, "num": num}


@app.post("/api/comments/{cid}/reply")
def reply_comment(cid: int, body: str = Form(...)):
    parent = db.one("SELECT * FROM comment WHERE id=?", (cid,))
    if not parent:
        raise HTTPException(404, "comment not found")
    num = _next_comment_num(parent["output_id"])
    rid = db.insert("comment", project_id=parent["project_id"], output_id=parent["output_id"],
                    num=num, title="", body=body, source="reply",
                    parent_id=cid, created_at=db.now_iso())
    return {"id": rid, "num": num}


@app.post("/api/comments/{cid}/resolve")
def resolve_comment(cid: int, resolved: int = Form(1)):
    c = db.one("SELECT * FROM comment WHERE id=?", (cid,))
    db.execute("UPDATE comment SET resolved=? WHERE id=?", (1 if resolved else 0, cid))
    if c and resolved:
        lbl = ""
        if c.get("output_id"):
            o = db.one("SELECT label FROM output WHERE id=?", (c["output_id"],))
            lbl = (o or {}).get("label") or ""
        db.log_comment_action("Reviewer", "comment_resolve", c["project_id"], lbl, c.get("body") or "")
    return {"ok": True}


@app.delete("/api/comments/{cid}")
def delete_comment(cid: int):
    db.execute("DELETE FROM comment WHERE id=?", (cid,))
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #

@app.get("/api/projects/{pid}/export/comments.xlsx")
def export_comments(pid: int):
    data = export.comments_xlsx(pid)
    return Response(
        data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=_content_headers("review_comments.xlsx"),
    )


@app.post("/api/projects/{pid}/import-comments")
async def import_comments(pid: int, file: UploadFile = File(...)):
    """Re-import an edited comments sheet (ID | Table | Comment | Reply to | Resolved). (ID, Table) rows replace matching comments; a new (ID, Table) with a known Table creates one. The whole sheet is validated first — any error aborts with a 400 and writes nothing."""
    if not db.one("SELECT id FROM project WHERE id=?", (pid,)):
        raise HTTPException(404, "project not found")
    data = await _read_upload_limited(file, MAX_SHEET_BYTES, "comments workbook")
    try:
        summary = export.import_comments_xlsx(pid, data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit("Reviewer", "comments.import", "project", pid, pid,
             f"{summary['updated']} updated / {summary['created']} created from {file.filename}")
    return summary


# --------------------------------------------------------------------------- #
# AI review
# --------------------------------------------------------------------------- #

@app.get("/api/ai/available")
def ai_available():
    return {"available": ai_client.available()}


@app.get("/api/ai/models")
def ai_models():
    """Selectable models + effort levels for the AI Review toolbar. Models are discovered live from the current user's API key (`ai_client.available_models`), so the list reflects exactly what that key grants."""
    return {"models": ai_client.available_models(), "efforts": ai_client.EFFORTS,
            "default_model": ai_client.default_model(),
            "default_effort": ai_client.run_config()["effort"]}


# Rough per-output wall-clock (seconds) by model family, scaled by effort, for the pre-run time estimate. Family-based so it works for any discovered id (dated releases, future versions). Deliberately conservative; shown as a range.
_EFFORT_MULT = {"low": 1.0, "medium": 1.5, "high": 2.5, "xhigh": 3.5, "max": 5.0}


def _model_secs(model: str) -> float:
    m = (model or "").lower()
    if "opus" in m:
        return 22
    if "haiku" in m:
        return 6
    if "fable" in m:
        return 9
    return 12  # sonnet and anything else


def _fmt_dur(s: float) -> str:
    s = int(round(s))
    return f"{s}s" if s < 90 else f"{max(1, round(s / 60))}m"


@app.get("/api/projects/{pid}/ai-estimate")
def ai_estimate(pid: int, model: str = "", effort: str = ""):
    """Pre-run time estimate. Predicting LLM wall-clock is inherently noisy, so the result
    is a WIDE range. When the project has completed runs we calibrate against their ACTUAL
    seconds-per-target (runner.measured_throughput) — the only signal that captures real
    gateway latency, retries and effective concurrency, all of which the modelled constants
    chronically under-count. With no history we fall back to a deliberately conservative
    model:
      1. Cache — extraction (the bulk of a cold run) is skipped for outputs already done,
         so a warm re-run is dominated by re-judging, not re-reading.
      2. Concurrency — up to MAX_INFLIGHT calls overlap, but rate limiting keeps the real
         overlap well below the nominal ceiling.
      3. Cross-output judging is several chunked calls, not one.
    """
    info = runner.count_targets(pid)
    n = info.get("targets", 0)
    if not n:
        return {"targets": 0, "seconds": 0, "text": "No tables to review."}

    warm = info.get("cached", 0)
    detail = f"{n} table{'' if n == 1 else 's'}"
    if warm:
        detail += f", {warm} already extracted"      # why a re-run is much faster
    if info.get("skipped"):
        detail += f" (+{info['skipped']} non-table skipped)"

    # Calibrated path: this project's own measured seconds-per-target beats any model.
    spt = runner.measured_throughput(pid)
    if spt:
        secs = spt * n
        text = f"Rough estimate {_fmt_dur(secs * 0.7)}–{_fmt_dur(secs * 1.6)} · {detail} · from past runs"
        return {"targets": n, "seconds": round(secs), "cached": warm,
                "calibrated": True, "text": text}

    # First-run fallback: modelled, conservative, wide range. With nothing to anchor on we bias high — an under-estimate ("said 20m, took 3h") is the far worse surprise.
    base = _model_secs(model)
    mult = _EFFORT_MULT.get(effort, 2.5)
    # Extraction: only the UNCACHED pages actually run, on the fast model.
    extract_calls = info.get("extract_calls", 0)
    extract_secs = extract_calls * _model_secs(ai_review._extract_model() or model)
    # Judges always re-run (findings are cleared and re-derived every run): one per table, plus the chunked cross-output pass (~1 call per ~12 tables, hub repeated).
    xout_calls = max(1, round(n / 12))
    judge_secs = (n + xout_calls) * base * mult
    par = max(1.0, ai_client.MAX_INFLIGHT * 0.5)   # rate limiting keeps real overlap low
    secs = (extract_secs + judge_secs) / par + 8   # + structural checks / bookkeeping
    text = f"Rough first-run estimate {_fmt_dur(secs * 0.6)}–{_fmt_dur(secs * 2.2)} · {detail}"
    return {"targets": n, "seconds": round(secs), "cached": warm, "text": text}


@app.post("/api/projects/{pid}/ai-run")
def ai_run(pid: int, kind: str = "incremental", actor: str = "Reviewer",
           model: str = Form(""), effort: str = Form("")):
    if not ai_client.available():
        raise HTTPException(400, "ANTHROPIC_API_KEY / anthropic SDK not available")
    try:
        result = runner.start_run(pid, kind, model=model or None, effort=effort or None)
    except runner.RunAlreadyActive as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if result.get("error"):
        raise HTTPException(400, result["error"])
    db.audit(actor, "ai_run.start", "project", pid, pid,
             f"kind={kind} model={result.get('model', model)} "
             f"effort={result.get('effort', effort)} run_id={result.get('run_id')}")
    return result


@app.get("/api/projects/{pid}/audit")
def get_audit(pid: int):
    return db.query("SELECT * FROM audit_log WHERE project_id=? ORDER BY id DESC", (pid,))


@app.get("/api/projects/{pid}/ai-progress")
def ai_progress(pid: int):
    return runner.RUN_PROGRESS.get(pid, {"running": False, "done": 0, "total": 0, "message": "idle"})


def _finding_scope(check_id: str, output_id) -> str:
    """Prefix-based scope for the UI. AIV-* (version judge) and XOUT-020 (missing vs prior) compare against the prior edition; AIX-* (cross-output judge), XOUT-001 (numbering gap) and any finding without an owning output are cross-output; the rest live within a single file."""
    cid = check_id or ""
    if cid.startswith("AIV-") or cid == "XOUT-020":
        return "cross-file"
    if cid.startswith("AIX-") or cid == "XOUT-001" or output_id is None:
        return "cross-output"
    return "within-file"


@app.get("/api/projects/{pid}/findings")
def list_findings(pid: int):
    rows = db.query(
        """SELECT f.*, o.label AS output_label, d.filename AS file
           FROM finding f
           LEFT JOIN output o ON o.id=f.output_id
           LEFT JOIN document d ON d.id=o.document_id
           WHERE f.project_id=? ORDER BY
             -- High tier first. Accepts legacy values (critical/major) alongside the
             -- current two-tier 'high'/'low' so old findings still sort correctly.
             CASE WHEN f.severity IN ('high', 'critical', 'major') THEN 0 ELSE 1 END,
             -- Then by output and check, so ordering is STABLE regardless of the order
             -- findings were written (workers now persist them in completion order).
             COALESCE(o.seq, 1000000), f.check_id, f.id""", (pid,))
    for r in rows:
        r["subjects"] = db.loads(r["subjects"], [])
        r["numbers"] = db.loads(r["numbers"], [])
        r["affected"] = db.loads(r["affected"], [])
        r["scope"] = _finding_scope(r["check_id"], r["output_id"])
    return rows


@app.post("/api/findings/{fid}/{action}")
def finding_action(fid: int, action: str, text: str = Form(""), author: str = Form("Reviewer")):
    f = db.one("SELECT * FROM finding WHERE id=?", (fid,))
    if not f:
        raise HTTPException(404, "finding not found")
    if action == "post":
        cid = db.insert("comment", project_id=f["project_id"], output_id=f["output_id"],
                        title=f["check_id"], body=text or f["message"], source="ai",
                        finding_id=fid, author=author, created_at=db.now_iso())
        db.execute("UPDATE finding SET state='posted' WHERE id=?", (fid,))
        if f["output_id"]:
            db.execute("UPDATE output SET status='In Progress' WHERE id=? AND status='Not Reviewed'",
                       (f["output_id"],))
        db.audit(author, "finding.post", "finding", fid, f["project_id"], f["check_id"])
        db.log_finding_action(author, "post", f, comment_text=text or f["message"])
        return {"ok": True, "comment_id": cid}
    if action == "reject":
        db.execute("UPDATE finding SET state='rejected' WHERE id=?", (fid,))
        db.audit(author, "finding.reject", "finding", fid, f["project_id"], f["check_id"])
        db.log_finding_action(author, "reject", f, comment_text=text)
        return {"ok": True}
    if action == "reopen":
        db.execute("UPDATE finding SET state='pending' WHERE id=?", (fid,))
        db.execute("DELETE FROM comment WHERE finding_id=?", (fid,))
        db.audit(author, "finding.reopen", "finding", fid, f["project_id"], f["check_id"])
        db.log_finding_action(author, "reopen", f, comment_text=text)
        return {"ok": True}
    raise HTTPException(400, f"unknown action {action}")


@app.get("/api/projects/{pid}/ai-last-run")
def ai_last_run(pid: int):
    run = db.one("SELECT * FROM ai_run WHERE project_id=? ORDER BY id DESC LIMIT 1", (pid,))
    if not run:
        return {"none": True}
    run["summary"] = db.loads(run.get("summary_json"), {})
    return run


@app.post("/api/outputs/{oid}/ai-run")
def ai_run_output(oid: int):
    """Re-run the AI review for a single output (used by the per-output Re-run button)."""
    if not ai_client.available():
        raise HTTPException(400, "AI not available")
    try:
        result = runner.run_single_output(oid)
    except runner.RunAlreadyActive as exc:
        raise HTTPException(409, str(exc)) from exc
    if result.get("error"):
        raise HTTPException(502, result["error"])
    return result


@app.delete("/api/projects/{pid}/findings")
def clear_findings(pid: int):
    db.execute("DELETE FROM finding WHERE project_id=?", (pid,))
    return {"ok": True}


@app.get("/api/projects/{pid}/export/annotated.pdf")
def export_annotated(pid: int):
    data = export.annotated_pdf(pid)
    return Response(data, media_type="application/pdf",
                    headers=_content_headers("annotated_review.pdf"))


@app.get("/api/projects/{pid}/export/findings.xlsx")
def export_findings_xlsx(pid: int):
    data = export.findings_xlsx(pid)
    return Response(
        data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=_content_headers("ai_findings.xlsx"),
    )


@app.post("/api/projects/{pid}/import-findings")
async def import_findings(pid: int, file: UploadFile = File(...), actor: str = "Reviewer"):
    """Load findings from a structured .xlsx (the reverse of the export). Non-destructive: imported findings are ADDED alongside existing ones (phase 'imported')."""
    if not db.one("SELECT id FROM project WHERE id=?", (pid,)):
        raise HTTPException(404, "project not found")
    data = await _read_upload_limited(file, MAX_SHEET_BYTES, "findings workbook")
    try:
        summary = export.import_findings_xlsx(pid, data, actor=actor)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit(actor, "findings.import", "project", pid, pid,
             f"{summary['imported']} findings from {file.filename} "
             f"({summary.get('skipped_deterministic', 0)} deterministic skipped, "
             f"{summary.get('auto_approved', 0)} clean tables auto-approved)")
    return summary


# --------------------------------------------------------------------------- #
# Annotations (PDF marks: highlight / rectangle / freehand)
# --------------------------------------------------------------------------- #

@app.get("/api/outputs/{oid}/annotations")
def list_annotations(oid: int):
    return db.query("SELECT * FROM annotation WHERE output_id=? ORDER BY id", (oid,))


@app.post("/api/outputs/{oid}/annotations")
def add_annotation(oid: int, kind: str = Form(...), page: int = Form(...),
                   geom_json: str = Form(...), note: str = Form("")):
    output = db.one("SELECT page_start, page_end FROM output WHERE id=?", (oid,))
    if not output:
        raise HTTPException(404, "output not found")
    try:
        geom_json = project_io.validate_annotation_payload(
            kind, page, geom_json, output["page_end"] - output["page_start"] + 1,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    aid = db.insert("annotation", output_id=oid, kind=kind, page=page,
                    geom_json=geom_json, note=note, created_at=db.now_iso())
    return {"id": aid}


@app.delete("/api/annotations/{aid}")
def delete_annotation(aid: int):
    db.execute("DELETE FROM annotation WHERE id=?", (aid,))
    return {"ok": True}


@app.post("/api/chat")
def chat_endpoint(scope: str = Form(...), question: str = Form(...),
                  project_id: int = Form(None), output_id: int = Form(None)):
    if not ai_client.available():
        raise HTTPException(400, "AI not available")
    if scope == "output" and output_id:
        return {"answer": chat.ask_output(output_id, question)}
    return {"answer": chat.ask_global(project_id, question)}


# --------------------------------------------------------------------------- #
# Static SPA (mounted last so /api/* wins)
# --------------------------------------------------------------------------- #

# Serve the tutorial with a revalidation header so the browser never shows a stale cached copy after it is updated. StaticFiles sets only last-modified/etag (no Cache-Control), which lets browsers apply heuristic freshness and keep showing the OLD Help page. "no-cache" = always revalidate (cheap 304 when unchanged, fresh 200 right after any edit). Defined before the "/" mount so it takes precedence.
@app.get("/tutorial.html")
def tutorial():
    return FileResponse(
        os.path.join(STATIC, "tutorial.html"),
        media_type="text/html",
        headers={"Cache-Control": "no-cache"},
    )


class _NoCacheStatic(StaticFiles):
    """Serve every SPA asset with 'Cache-Control: no-cache' so the browser always revalidates (cheap 304 when unchanged, fresh 200 right after an edit) instead of showing a stale app.js / styles.css / index.html from heuristic cache. This extends the /tutorial.html fix above to the whole mount."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/", _NoCacheStatic(directory=STATIC, html=True), name="static")
