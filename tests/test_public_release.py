from pathlib import Path
import subprocess

import pytest

import public_release as release


def test_private_and_generated_paths_are_excluded():
    assert release._excluded(Path("study_config.json"))
    assert release._excluded(Path("data/app.db"))
    assert release._excluded(Path("runtime/python.exe"))
    assert release._excluded(Path("demo/artifacts/run/example.pdf"))
    assert release._excluded(Path(".Rhistory"))
    assert not release._excluded(Path("configs/study_config.synthetic.json"))


def test_scanner_detects_blocked_identifier_without_echoing_it_in_fixture_source(tmp_path):
    value = "abb" + "vie"
    candidate = release.Candidate(tmp_path / "fixture.txt", "fixture.txt", value.encode())
    findings = release.scan_candidates([candidate])
    assert any(item.rule == "employer identifier" for item in findings)


def test_only_hash_pinned_reviewed_images_are_allowed(tmp_path):
    path = "docs/images/01_dashboard.png"
    approved = release.Candidate(tmp_path / "approved.png", path,
                                 (release.ROOT / path).read_bytes())
    assert release.scan_candidates([approved]) == []

    changed = release.Candidate(tmp_path / "changed.png", path, approved.data + b"changed")
    findings = release.scan_candidates([changed])
    assert any(item.rule == "reviewed binary hash mismatch" for item in findings)


def test_source_digest_is_order_independent(tmp_path):
    first = release.Candidate(tmp_path / "a", "a.txt", b"alpha")
    second = release.Candidate(tmp_path / "b", "b.txt", b"beta")
    assert release.source_digest([first, second]) == release.source_digest([second, first])


def test_collection_substitutes_only_the_synthetic_root_config():
    candidates, _findings, _excluded = release.collect_candidates()
    root_configs = [item for item in candidates if item.archive_path == "study_config.json"]
    assert len(root_configs) == 1
    assert root_configs[0].source == release.SYNTHETIC_CONFIG
    assert root_configs[0].data == release.SYNTHETIC_CONFIG.read_bytes()


def test_collection_fails_closed_when_required_public_files_are_missing(tmp_path):
    config = tmp_path / "configs" / "study_config.synthetic.json"
    config.parent.mkdir()
    config.write_text("{}\n", encoding="utf-8")
    _candidates, findings, _excluded = release.collect_candidates(tmp_path)
    assert any(
        item.path == "README.md" and item.rule == "required public file is missing"
        for item in findings
    )


def test_strict_collection_rejects_excluded_staged_file(tmp_path):
    config = tmp_path / "configs" / "study_config.synthetic.json"
    config.parent.mkdir()
    config.write_text("{}\n", encoding="utf-8")
    (tmp_path / ".Rhistory").write_text("private local session\n", encoding="utf-8")
    _candidates, findings, _excluded = release.collect_candidates(
        tmp_path, strict_exclusions=True
    )
    assert any(
        item.path == ".Rhistory" and item.rule == "excluded file is staged" for item in findings
    )


def test_workflow_actions_require_full_commit_sha(tmp_path):
    unpinned = release.Candidate(
        tmp_path / "ci.yml", ".github/workflows/ci.yml", b"steps:\n  - uses: actions/checkout@v7\n"
    )
    findings = release.scan_candidates([unpinned])
    assert any(item.rule == "GitHub Action is not pinned to a full commit SHA" for item in findings)

    pinned = release.Candidate(
        tmp_path / "ci.yml",
        ".github/workflows/ci.yml",
        (
            "steps:\n  - uses: actions/checkout@"
            "3d3c42e5aac5ba805825da76410c181273ba90b1\n"
        ).encode(),
    )
    assert release.scan_candidates([pinned]) == []
    mixed = release.Candidate(
        tmp_path / "Dockerfile",
        "Dockerfile",
        ("FROM python:3.12@sha256:" + "a" * 64 + " AS build\nFROM python:3.12\n").encode(),
    )
    assert any(
        item.line == 2 and item.rule == "container base image is not pinned by SHA-256 digest"
        for item in release.scan_candidates([mixed])
    )

    unknown = release.Candidate(
        tmp_path / "ci.yml",
        ".github/workflows/ci.yml",
        ("steps:\n  - uses: example/action@" + "a" * 40 + "\n").encode(),
    )
    assert any(
        item.rule == "GitHub Action pin is not reviewed"
        for item in release.scan_candidates([unknown])
    )


def test_lock_inventory_requires_exact_unique_versions(tmp_path):
    candidate = release.Candidate(
        tmp_path / "requirements-lock.txt",
        "requirements-lock.txt",
        b"fastapi>=0.1\nfastapi==1.0\nFastAPI==1.0\n",
    )
    findings = release.scan_candidates([candidate])
    assert any(item.rule == "lock entry is not an exact version pin" for item in findings)
    assert any(item.rule == "duplicate package in lock inventory" for item in findings)


def test_lock_inventory_must_cover_recursive_direct_requirements(tmp_path):
    candidates = [
        release.Candidate(
            tmp_path / "requirements-demo-lock.txt",
            "requirements-demo-lock.txt",
            b"fastapi==1.0\n",
        ),
        release.Candidate(
            tmp_path / "requirements-demo.txt",
            "requirements-demo.txt",
            b"-r requirements.txt\nreportlab>=4\n",
        ),
        release.Candidate(
            tmp_path / "requirements.txt",
            "requirements.txt",
            b"fastapi>=0.1\n",
        ),
    ]
    findings = release.scan_candidates(candidates)
    assert any(
        item.rule == "direct requirement is missing from exact lock" and item.excerpt == "reportlab"
        for item in findings
    )


def test_active_svg_content_is_rejected(tmp_path):
    candidate = release.Candidate(
        tmp_path / "active.svg",
        "active.svg",
        b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
    )
    findings = release.scan_candidates([candidate])
    assert any(item.rule == "active SVG element is not allowed" for item in findings)


def test_container_base_must_be_digest_pinned(tmp_path):
    floating = release.Candidate(tmp_path / "Dockerfile", "Dockerfile", b"FROM python:3.12-slim\n")
    assert any(
        item.rule == "container base image is not pinned by SHA-256 digest"
        for item in release.scan_candidates([floating])
    )
    pinned = release.Candidate(
        tmp_path / "Dockerfile",
        "Dockerfile",
        ("FROM python:3.12-slim@sha256:" + "a" * 64 + "\n").encode(),
    )
    assert release.scan_candidates([pinned]) == []


def test_staged_tree_reads_index_not_unstaged_worktree(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("staged\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    tracked.write_text("unstaged\n", encoding="utf-8")
    monkeypatch.setattr(release, "ROOT", tmp_path)

    with release.staged_tree() as staged:
        assert (staged / "tracked.txt").read_text(encoding="utf-8") == "staged\n"


def test_history_scanner_detects_removed_secret(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True)
    tracked = tmp_path / "secret.txt"
    tracked.write_text("github" + "_pat_" + "A" * 30 + "\n", encoding="utf-8")
    subprocess.run(["git", "add", "secret.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "temporary credential"], cwd=tmp_path, check=True)
    tracked.write_text("removed\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "remove credential"], cwd=tmp_path, check=True)
    monkeypatch.setattr(release, "ROOT", tmp_path)

    findings = release.git_history_findings()
    assert any(item.rule == "possible GitHub fine-grained token" for item in findings)
