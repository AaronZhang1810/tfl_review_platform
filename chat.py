"""Chat scopes: global (whole delivery) and per-output.

Both build a compact context from stored data and ask Claude. SAP/Protocol text, when present, is attached as prompt-cached system blocks."""

from __future__ import annotations

import json

import ai_client
import db
import pdftools

_MAX_DOC = 40000


def _study_doc_blocks(project_id: int) -> list[dict]:
    blocks = []
    for role in ("sap", "protocol"):
        doc = db.one("SELECT * FROM document WHERE project_id=? AND role=?", (project_id, role))
        if doc:
            try:
                text = pdftools.range_text(doc["path"], 1, doc["n_pages"], max_chars=_MAX_DOC)
                blocks.append(ai_client.cached_system_block(
                    f"{role.upper()} document ({doc['filename']}):\n{text}"))
            except Exception:
                pass
    return blocks


def _findings_digest(project_id: int, output_id: int | None = None) -> str:
    sql = "SELECT check_id, severity, message, state FROM finding WHERE project_id=?"
    params = [project_id]
    if output_id is not None:
        sql += " AND output_id=?"; params.append(output_id)
    rows = db.query(sql, tuple(params))
    return json.dumps(rows, ensure_ascii=False)[:20000]


def ask_global(project_id: int, question: str) -> str:
    outs = db.query("SELECT label, title, status FROM output WHERE project_id=? ORDER BY seq", (project_id,))
    system = [{"type": "text", "text":
        "You are an assistant for a clinical TLF review. Answer ONLY from the provided "
        "delivery context (output list + AI findings). Be concise; cite table labels."}]
    system += _study_doc_blocks(project_id)
    user = (f"Output list:\n{json.dumps(outs, ensure_ascii=False)[:20000]}\n\n"
            f"AI findings:\n{_findings_digest(project_id)}\n\n"
            f"Question: {question}")
    return ai_client.call_text(system, user, model=ai_client.FAST_MODEL)


def ask_output(output_id: int, question: str) -> str:
    o = db.one("SELECT * FROM output WHERE id=?", (output_id,))
    if not o:
        return "Output not found."
    system = [{"type": "text", "text":
        "You are an assistant for a clinical TLF review, scoped to ONE output. Answer "
        "only from its extraction and findings."}]
    system += _study_doc_blocks(o["project_id"])
    user = (f"Output: {o['label']} — {o['title']}\n\n"
            f"Extraction:\n{(o['extraction_json'] or '{}')[:20000]}\n\n"
            f"Findings:\n{_findings_digest(o['project_id'], output_id)}\n\n"
            f"Question: {question}")
    return ai_client.call_text(system, user, model=ai_client.FAST_MODEL)
