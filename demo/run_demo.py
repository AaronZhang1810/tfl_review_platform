#!/usr/bin/env python3
"""Prepare and optionally launch the isolated synthetic TLF-review demo.

This entry point never imports the application until TLF_DATA_DIR and
TLF_DEMO_MODE have been set. Every invocation creates a new run directory, so the
normal application database and uploaded documents cannot be overwritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timezone
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "demo" / "artifacts"
DISCLAIMER = "SYNTHETIC DEMO — no real clinical or patient data; AI behavior is simulated."


def _scenarios() -> list[dict]:
    """Fictional one-page tables. Values are deliberately simple and auditable."""
    return [
        {
            "number": "1", "title": "Analysis Population by Assigned Group",
            "row": "Participants in analysis population", "prior": [110, 108, 218],
            "current": [120, 118, 237],
            "findings": [
                ("AIW-2.1", "High", "Treatment A (120) + Treatment B (118) = 238, but Total is printed as 237.", [120, 118, 237]),
                ("AIX-4", "High", "Table 1 reports Total N=237 while the by-study exposure table reports N=238.", [237, 238]),
            ],
        },
        {
            "number": "2.1", "title": "Participant Disposition",
            "row": "Completed study", "prior": [101, 100, 201], "current": [111, 109, 220],
            "findings": [
                ("AIW-2.3", "Low", "The title says Safety Population but the footnote defines the Randomized Population.", []),
            ],
        },
        {
            "number": "2.2.1", "title": "Overview of Treatment-Emergent Adverse Events",
            "row": "Participants with any TEAE", "prior": [86, 83, 169], "current": [94, 91, 185],
            "pct_override": [78.3, 77.1, 77.7],
            "findings": [
                ("AIW-2.3", "High", "Treatment A prints 94/120 as 82.3%; the calculated percentage is 78.3%.", [94, 120, 82.3, 78.3]),
                ("AIV-7.3", "Low", "The TEAE overview definition changed from the prior edition without a revision note.", []),
            ],
        },
        {
            "number": "2.2.2", "title": "Treatment-Emergent Adverse Events by SOC and PT",
            "row": "Any treatment-emergent adverse event", "prior": [86, 83, 169], "current": [94, 90, 184],
            "findings": [
                ("AIX-7.1", "High", "The SOC/PT table reports 184 participants with any TEAE, versus 185 in the AE overview.", [184, 185]),
            ],
        },
        {
            "number": "3.1", "title": "Potentially Clinically Significant Laboratory Values",
            "row": "At least one clinically significant result", "prior": [15, 17, 32], "current": [18, 19, 37],
            "findings": [],
        },
        {
            "number": "3.2", "title": "Potentially Clinically Significant Vital Signs",
            "row": "At least one clinically significant result", "prior": [11, 13, 24], "current": [121, 14, 135],
            "findings": [
                ("AIW-2.3", "High", "Treatment A has n=121, which exceeds its group denominator N=120.", [121, 120]),
            ],
        },
        {
            "number": "4.1", "title": "Best Overall Response",
            "row": "Participants with response", "prior": [72, 68, 140], "current": [79, 72, 152],
            "findings": [
                ("AIW-2.2", "High", "Treatment components 79 + 72 = 151, but Total is printed as 152.", [79, 72, 152]),
            ],
        },
        {
            "number": "4.2", "title": "Time-to-Event Analysis",
            "row": "Participants with an observed event", "prior": [36, 34, 70], "current": [34, 32, 66],
            "findings": [
                ("AIV-6.2", "High", "Observed-event count decreased from 70 in the prior edition to 66 in the current edition.", [70, 66]),
            ],
        },
        {
            "number": "5.1", "title": "Study-Drug Exposure by Study",
            "row": "Participants exposed", "prior": [110, 108, 218], "current": [120, 118, 238],
            "findings": [
                ("AIX-8", "High", "Exposure Total N=238 does not match the analysis-population Total N=237 in Table 1.", [238, 237]),
            ],
        },
        {
            "number": "5.2", "title": "Dose Modifications",
            "row": "Participants with dose interruption", "prior": [10, 11, 21], "current": [13, 14, 27],
            "findings": [],
        },
        {
            "number": "6.1", "title": "Concomitant Medication Use",
            "row": "Participants with concomitant medication", "prior": [77, 75, 152], "current": [84, 83, 167],
            "footnote": "Pooled studies: SYN-101 and SYN-102.",
            "findings": [
                ("AIX-5", "Low", "The footnote includes SYN-102, but the configured pool contains only fictional study SYN-101.", []),
            ],
        },
        {
            "number": "7.1", "title": "Response by Region",
            "row": "Region East — participants with response", "prior": [34, 31, 65], "current": [38, 36, 74],
            "findings": [],
        },
    ]


def _draw_pdf(path: Path, edition: str, scenarios: list[dict]) -> None:
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas
    except ImportError as exc:  # pragma: no cover - exercised by launcher smoke check
        raise SystemExit(
            "The synthetic demo needs reportlab. Run: pip install -r requirements-demo.txt"
        ) from exc

    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=letter, pageCompression=1)
    width, height = letter
    for idx, item in enumerate(scenarios, 1):
        key = f"table-{item['number'].replace('.', '-')}"
        c.bookmarkPage(key)
        c.addOutlineEntry(f"Table {item['number']} {item['title']}", key, level=0)

        c.setFillColor(colors.HexColor("#8b1e2d"))
        c.rect(0, height - 38, width, 38, fill=1, stroke=0)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 10)
        c.drawCentredString(width / 2, height - 24, DISCLAIMER)

        c.setFillColor(colors.black)
        c.setFont("Helvetica-Bold", 13)
        c.drawCentredString(width / 2, height - 72, f"TABLE {item['number']}")
        c.setFont("Helvetica-Bold", 11)
        c.drawCentredString(width / 2, height - 90, item["title"])
        c.setFont("Helvetica", 9)
        c.drawCentredString(width / 2, height - 106, f"SYN-101 · {edition} synthetic edition · Safety Analysis Set")

        left, top = 52, height - 145
        widths = [270, 82, 82, 82]
        headers = ["Row", "Treatment A\nN=120", "Treatment B\nN=118", "Total\nN=238"]
        c.setFillColor(colors.HexColor("#e8eef7"))
        c.rect(left, top - 38, sum(widths), 38, fill=1, stroke=0)
        x = left
        for j, text in enumerate(headers):
            c.setStrokeColor(colors.HexColor("#7b8794"))
            c.rect(x, top - 38, widths[j], 38, fill=0, stroke=1)
            c.setFillColor(colors.black)
            c.setFont("Helvetica-Bold", 8)
            parts = text.split("\n")
            for k, part in enumerate(parts):
                c.drawCentredString(x + widths[j] / 2, top - 15 - 11 * k, part)
            x += widths[j]

        values = item["current"] if edition == "2026" else item["prior"]
        denominators = [120, 118, 238] if edition == "2026" else [110, 108, 218]
        pcts = [round(100 * n / N, 1) for n, N in zip(values, denominators)]
        if edition == "2026" and item.get("pct_override"):
            pcts = item["pct_override"]
        rows = [
            (item["row"], [f"{n} ({p:.1f}%)" for n, p in zip(values, pcts)]),
            ("No event / not meeting criterion", [str(max(N - min(n, N), 0)) for n, N in zip(values, denominators)]),
            ("Missing assessment", ["0", "0", "0"]),
        ]
        y = top - 38
        for ridx, (label, cells) in enumerate(rows):
            y -= 31
            c.setFillColor(colors.HexColor("#f8fafc") if ridx % 2 else colors.white)
            c.rect(left, y, sum(widths), 31, fill=1, stroke=0)
            x = left
            row_cells = [label] + cells
            for j, text in enumerate(row_cells):
                c.setStrokeColor(colors.HexColor("#a7b0ba"))
                c.rect(x, y, widths[j], 31, fill=0, stroke=1)
                c.setFillColor(colors.black)
                c.setFont("Helvetica", 8)
                if j == 0:
                    c.drawString(x + 5, y + 11, text[:58])
                else:
                    c.drawCentredString(x + widths[j] / 2, y + 11, text)
                x += widths[j]

        c.setFont("Helvetica", 8)
        c.setFillColor(colors.HexColor("#334155"))
        c.drawString(left, y - 24, item.get("footnote", "Percentages use the column header N as denominator."))
        c.drawString(left, y - 38, "TEAE = treatment-emergent adverse event. All names and values are fictional.")
        c.setFillColor(colors.HexColor("#8b1e2d"))
        c.setFont("Helvetica-Bold", 24)
        c.saveState()
        c.translate(width / 2, height / 2 - 70)
        c.rotate(28)
        c.setFillAlpha(0.08)
        c.drawCentredString(0, 0, "SYNTHETIC — NOT FOR CLINICAL USE")
        c.restoreState()
        c.setFillColor(colors.black)
        c.setFont("Helvetica", 8)
        c.drawRightString(width - 45, 30, f"Page {idx} of {len(scenarios)} · generated demo artifact")
        c.showPage()
    c.save()


def _extraction(item: dict, edition: str) -> dict:
    vals = item["current"] if edition == "2026" else item["prior"]
    denoms = [120, 118, 238] if edition == "2026" else [110, 108, 218]
    labels = ["Treatment A", "Treatment B", "Total"]
    pcts = [round(100 * n / N, 1) for n, N in zip(vals, denoms)]
    if edition == "2026" and item.get("pct_override"):
        pcts = item["pct_override"]
    return {
        "analysis_set": "Safety Analysis Set",
        "groups": [{"label": label, "n": N} for label, N in zip(labels, denoms)],
        "summary_rows": [{
            "label": item["row"], "page": 1,
            "values": dict(zip(labels, vals)), "pcts": dict(zip(labels, pcts)),
        }],
        "footnote_markers": [item.get("footnote", "")],
        "pt_terms": [], "missing_n_rows": [], "notes": "Synthetic seeded extraction",
        "coverage": {"pages_total": 1, "pages_read": 1, "slices_total": 1,
                     "slices_used": 1, "slices_ok": 1, "truncated": False,
                     "incomplete": False, "read_errors": []},
    }


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def prepare(data_dir: Path) -> dict:
    os.environ["TLF_DATA_DIR"] = str(data_dir)
    os.environ["TLF_DEMO_MODE"] = "1"
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    docs_dir = data_dir / "uploads" / "1"
    current_pdf = docs_dir / "SYN-101_TLF_2026_SYNTHETIC.pdf"
    prior_pdf = docs_dir / "SYN-101_TLF_2025_SYNTHETIC.pdf"
    scenarios = _scenarios()
    _draw_pdf(current_pdf, "2026", scenarios)
    _draw_pdf(prior_pdf, "2025", scenarios)

    import db
    import checks

    db.init()
    pid = db.insert(
        "project", compound="SYN-417", study="SYN-101",
        name="Synthetic TLF QC Showcase", edition_label="2026 simulated review",
        created_at=db.now_iso(),
    )
    current_doc = db.insert(
        "document", project_id=pid, role="delivery", filename=current_pdf.name,
        path=str(current_pdf), n_pages=len(scenarios), edition="2026",
    )
    prior_doc = db.insert(
        "document", project_id=pid, role="prior", filename=prior_pdf.name,
        path=str(prior_pdf), n_pages=len(scenarios), edition="2025",
    )

    current_ids: dict[str, int] = {}
    for doc_id, edition in ((current_doc, "2026"), (prior_doc, "2025")):
        for seq, item in enumerate(scenarios):
            label = f"Table {item['number']}"
            has_finding = bool(item["findings"]) and edition == "2026"
            oid = db.insert(
                "output", project_id=pid, document_id=doc_id, seq=seq,
                output_type="Table", number=item["number"], label=label,
                title=item["title"], page_start=seq + 1, page_end=seq + 1,
                status="In Progress" if has_finding else "Not Reviewed",
                extraction_json=json.dumps(_extraction(item, edition)),
                content_hash=f"synthetic-{edition}-{item['number']}",
                src_hash=_sha256(current_pdf if edition == "2026" else prior_pdf),
                judge_key="synthetic-demo-v1" if edition == "2026" else None,
            )
            if edition == "2026":
                current_ids[label] = oid

    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    total_findings = sum(len(x["findings"]) for x in scenarios)
    run_id = db.insert(
        "ai_run", project_id=pid, kind="synthetic-simulation", started_at=started,
        finished_at=started,
        summary_json=json.dumps({
            "status": "succeeded", "review_complete": True, "synthetic": True,
            "targets": len(scenarios), "findings": total_findings, "skipped": 0,
            "errors": [], "n_failed": 0, "n_judge_failed": 0,
            "n_conn_errors": 0, "ai_unreachable": False, "auto_approved": 0,
            "coverage": {"pages_total": len(scenarios), "pages_read": len(scenarios),
                         "n_outputs": len(scenarios), "n_truncated": 0, "truncated": []},
            "disclaimer": DISCLAIMER,
        }),
    )

    finding_ids: list[int] = []
    for item in scenarios:
        label = f"Table {item['number']}"
        for check_id, risk, message, numbers in item["findings"]:
            scope = "cross" if check_id.startswith("AIX-") else "within"
            affected = [label]
            if check_id in {"AIX-4", "AIX-8"}:
                affected = ["Table 1", "Table 5.1"]
            elif check_id == "AIX-7.1":
                affected = ["Table 2.2.1", "Table 2.2.2"]
            fid = db.insert(
                "finding", project_id=pid, output_id=current_ids[label], run_id=run_id,
                check_id=check_id, severity=risk.lower(), risk=risk, message=message,
                subjects="[]", numbers=json.dumps(numbers), page=1, printed_page=1,
                pages_total=1, section="Synthetic example", row_kind="aggregate",
                signature=checks.finding_signature(check_id, label, numbers, message),
                state="pending", badge="new", phase=scope, affected=json.dumps(affected),
            )
            finding_ids.append(fid)

    # Seed two human decisions so the demo visibly includes review workflow/auditability.
    if finding_ids:
        first = db.one("SELECT * FROM finding WHERE id=?", (finding_ids[0],))
        db.execute("UPDATE finding SET state='posted', badge='' WHERE id=?", (finding_ids[0],))
        db.insert(
            "comment", project_id=pid, output_id=first["output_id"], title=first["check_id"],
            body="Confirmed against the synthetic source; send to programming for correction.",
            source="ai", finding_id=finding_ids[0], resolved=0, author="Demo Reviewer",
            num=1, created_at=db.now_iso(),
        )
        db.audit("Demo Reviewer", "finding.post", "finding", finding_ids[0], pid,
                 "Accepted synthetic finding after source verification")
    if len(finding_ids) > 1:
        db.execute("UPDATE finding SET state='rejected', badge='' WHERE id=?", (finding_ids[1],))
        db.audit("Demo Reviewer", "finding.reject", "finding", finding_ids[1], pid,
                 "Rejected synthetic duplicate during demonstration")
    db.audit("system", "demo.seed", "project", pid, pid, DISCLAIMER)

    manifest = {
        "schema_version": "1.0", "seed": 20260808, "project_id": pid,
        "data_dir": str(data_dir), "tables_per_edition": len(scenarios),
        "pages": len(scenarios) * 2, "injected_findings": total_findings,
        "real_patient_data": False, "external_ai_calls": False,
        "disclaimer": DISCLAIMER,
        "files": {
            current_pdf.name: _sha256(current_pdf), prior_pdf.name: _sha256(prior_pdf),
            "app.db": _sha256(Path(db.DB_PATH)),
        },
    }
    manifest_path = data_dir / "synthetic_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-open", action="store_true", help="Do not open a browser")
    parser.add_argument("--prepare-only", action="store_true", help="Generate artifacts and exit")
    parser.add_argument("--data-dir", type=Path, help="Explicit isolated directory (must not be production data)")
    args = parser.parse_args()

    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("Synthetic demo binds to loopback only; use --host 127.0.0.1")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    data_dir = (args.data_dir or ARTIFACTS / "runs" / f"{stamp}-{os.getpid()}").resolve()
    production_data = (ROOT / "data").resolve()
    if data_dir == production_data or production_data in data_dir.parents:
        raise SystemExit("Refusing to put demo artifacts in the production data directory")
    data_dir.mkdir(parents=True, exist_ok=False)
    manifest = prepare(data_dir)
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    if args.prepare_only:
        return 0

    os.environ["TLF_DATA_DIR"] = str(data_dir)
    os.environ["TLF_DEMO_MODE"] = "1"
    import uvicorn
    import main as application

    url = f"http://{args.host}:{args.port}/#project/1"
    if not args.no_open:
        # Let uvicorn begin binding while the default browser starts resolving localhost.
        import threading
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"\n{DISCLAIMER}\nDemo URL: {url}\nData: {data_dir}\n")
    uvicorn.run(application.app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
