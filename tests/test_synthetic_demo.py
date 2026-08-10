import json
from pathlib import Path
import sqlite3
import subprocess
import sys


def test_demo_prepare_is_isolated_reproducible_and_fictional(tmp_path):
    root = Path(__file__).resolve().parents[1]
    data_dir = tmp_path / "isolated-demo"
    proc = subprocess.run(
        [sys.executable, str(root / "demo" / "run_demo.py"),
         "--prepare-only", "--data-dir", str(data_dir)],
        cwd=root, text=True, capture_output=True, check=False, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    manifest = json.loads((data_dir / "synthetic_manifest.json").read_text(encoding="utf-8"))
    assert manifest["pages"] == 24
    assert manifest["tables_per_edition"] == 12
    assert manifest["injected_findings"] == 11
    assert manifest["real_patient_data"] is False
    assert manifest["external_ai_calls"] is False
    assert "SYNTHETIC DEMO" in manifest["disclaimer"]
    assert len(manifest["files"]) == 3
    assert all(len(digest) == 64 for digest in manifest["files"].values())

    database = data_dir / "app.db"
    with sqlite3.connect(database) as conn:
        assert conn.execute("select count(*) from project").fetchone()[0] == 1
        assert conn.execute("select count(*) from output").fetchone()[0] == 24
        assert conn.execute("select count(*) from finding").fetchone()[0] == 11
        summary = json.loads(conn.execute("select summary_json from ai_run").fetchone()[0])
        assert summary["synthetic"] is True
        assert summary["review_complete"] is True

    # The demo has its own database and never creates the normal application DB.
    assert data_dir.resolve() != (root / "data").resolve()
    assert all("SYN-" in p.name for p in (data_dir / "uploads" / "1").glob("*.pdf"))
