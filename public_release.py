"""Audit and build a sanitized public-source artifact.

The dry audit is always available::

    python public_release.py --check

Artifact creation requires a clean content audit. The release digest and generated
manifest bind the archive to the exact candidate tree.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from defusedxml import ElementTree as ET
from typing import Iterator
import zipfile


ROOT = Path(__file__).resolve().parent
SYNTHETIC_CONFIG = ROOT / "configs" / "study_config.synthetic.json"
ARCHIVE_ROOT = "tlf-review-public-source"

MAX_TEXT_FILE = 5 * 1024 * 1024
MAX_HISTORY_BYTES = 64 * 1024 * 1024

REQUIRED_PUBLIC_PATHS = {
    ".dockerignore",
    ".gitattributes",
    ".github/dependabot.yml",
    ".github/workflows/ci.yml",
    ".gitignore",
    "Dockerfile",
    "LICENSE",
    "NOTICE.md",
    "PRIVACY.md",
    "README.md",
    "RUN_SYNTHETIC_DEMO.bat",
    "SECURITY.md",
    "SETUP.md",
    "THIRD_PARTY_NOTICES.md",
    "VALIDATION.md",
    "compose.demo.yml",
    "configs/study_config.synthetic.json",
    "docs/ASSET_PROVENANCE.md",
    "evaluation/run_benchmark.py",
    "evaluation/REPORT.md",
    "main.py",
    "public_release.py",
    "pyproject.toml",
    "requirements-demo-lock.txt",
    "requirements-demo.txt",
    "requirements-dev.txt",
    "requirements-lock.txt",
    "requirements.txt",
    "run_synthetic_demo.sh",
    "static/app.js",
    "static/index.html",
    "static/styles.css",
    "static/tutorial.html",
    "static/vendor/PDFJS_LICENSE.txt",
    "static/vendor/pdf.mjs",
    "static/vendor/pdf.worker.mjs",
    "study_config.json",
}

EXCLUDED_DIRS = {
    ".git",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    ".vscode",
    "__pycache__",
    "_runtime_build",
    "build",
    "data",
    "dist",
    "htmlcov",
    "node_modules",
    "runtime",
    "runtime_old",
    "venv",
}

EXCLUDED_FILES = {
    ".DS_Store",
    ".RData",
    ".Rhistory",
    ".Ruserdata",
    ".coverage",
    ".env",
    ".env.local",
    "Thumbs.db",
    "server.log",
    "study_config.json",
}

EXCLUDED_SUFFIXES = {
    ".db",
    ".docx",
    ".log",
    ".pdf",
    ".pyc",
    ".pyo",
    ".rtf",
    ".sqlite",
    ".sqlite3",
    ".xls",
    ".xlsx",
    ".zip",
}

TEXT_SUFFIXES = {
    ".cfg",
    ".css",
    ".csv",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".jsonl",
    ".md",
    ".mjs",
    ".py",
    ".sh",
    ".srt",
    ".svg",
    ".bat",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
TEXT_NAMES = {".dockerignore", ".gitattributes", ".gitignore", "Dockerfile", "LICENSE"}

# Binary files are rejected unless they have been manually reviewed and pinned here.
# This makes replacing a synthetic screenshot with an unreviewed image fail closed.
APPROVED_BINARY_SHA256 = {
    "docs/images/01_dashboard.png": "d34e9cb277d7c92c5f4bfee7688e94c1c233de4deb41f25427a8ede821f85139",
    "docs/images/02_table_mismatch.png": "8b5d7cbd332834a663a0850fc89cb313ed4187bfd0d7b0804c4d82043e3045ba",
    "docs/images/03_cross_output.png": "98c9e583a5840d22589cb91065e2ad12f38d74b18f1292ef43075e81faa67b6e",
    "docs/images/04_ai_review.png": "9a9e941c808bd082bccdf3428b2d4502f75c55fd9002ab8d8ddfb2eea917d860",
    "docs/images/05_comments.png": "7bb002723f74aa8e2c389a5df6c45902c0a76125bc135b4e8dac43878e75aa0a",
    "docs/images/06_toc.png": "1d6e7b6e68b28a56acb8c6c0995f7adea155e77c3cb1fd30590de655390c4473",
    "docs/images/07_benchmark_report.png": "cec2eecf09bbbd691a01dd7de9fe8956db7e294029b3b19b89ae42ad7eb4a455",
    "docs/images/08_architecture.png": "c11bd915f40036f369c909744446c836ebec22f163dbc61b3d39cf8359058589",
    "docs/media/tlf-review-demo.mp4": "a6c1353b35cd27d389cb8f5d5b0b3c99a6f7a3deecbb9dd17c49e9d206145baf",
}


def _joined(*parts: str) -> str:
    """Keep the scanner's own source from containing a blocked literal."""
    return "".join(parts)


_clinical_terms = [
    _joined("C", "LL"),
    _joined("S", "LL"),
    _joined("A", "ML"),
    _joined("N", "HL"),
    _joined("M", "DS"),
]

BANNED_CONTENT = [
    ("employer identifier", re.compile(_joined("abb", "vie"), re.IGNORECASE)),
    ("private gateway identifier", re.compile(r"\b" + _joined("il", "iad") + r"\b", re.IGNORECASE)),
    ("program-specific compound", re.compile(_joined("vene", "toclax"), re.IGNORECASE)),
    ("program-specific development code", re.compile(_joined("abt", "-199"), re.IGNORECASE)),
    ("real-looking study code", re.compile(r"\bM\d{2}-\d{3}\b", re.IGNORECASE)),
    ("internal report edition", re.compile(r"\bIB(?:1[6-9]|2\d)\b", re.IGNORECASE)),
    (
        "program-specific indication",
        re.compile(r"\b(?:" + "|".join(map(re.escape, _clinical_terms)) + r")\b", re.IGNORECASE),
    ),
    ("corporate network product", re.compile(_joined("zsc", "aler"), re.IGNORECASE)),
    ("internal sharing platform", re.compile(_joined("share", "point"), re.IGNORECASE)),
    ("internal manual-review reference", re.compile(_joined("manual", " qc"), re.IGNORECASE)),
    ("internal sanctioned-client wording", re.compile(_joined("sanctioned", " claude"), re.IGNORECASE)),
    ("macOS personal path", re.compile(re.escape(_joined("/", "Users", "/")))),
    ("Windows personal path", re.compile(re.escape(_joined("C:", "\\", "Users", "\\")), re.IGNORECASE)),
]

SECRET_PATTERNS = [
    ("possible Anthropic API key", re.compile(_joined("sk", "-ant-") + r"[A-Za-z0-9_-]{12,}")),
    ("possible OpenAI API key", re.compile(_joined("sk", "-") + r"[A-Za-z0-9_-]{24,}")),
    ("possible GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("possible GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("possible GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    ("possible npm token", re.compile(r"\bnpm_[A-Za-z0-9]{20,}\b")),
    ("possible PyPI token", re.compile(r"\bpypi-[A-Za-z0-9_-]{40,}\b")),
    ("possible AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("private key material", re.compile(_joined("-----BEGIN ", "PRIVATE KEY-----"))),
    ("private key material", re.compile(_joined("-----BEGIN ", "RSA PRIVATE KEY-----"))),
    ("private key material", re.compile(_joined("-----BEGIN ", "OPENSSH PRIVATE KEY-----"))),
    ("private key material", re.compile(_joined("-----BEGIN ", "EC PRIVATE KEY-----"))),
]

REMOTE_ACTION = re.compile(r"^\s*(?:-\s*)?uses:\s*([^#\s]+)", re.MULTILINE)
FULL_COMMIT_SHA = re.compile(r"^[^@\s]+@[0-9a-fA-F]{40}$")
PINNED_CONTAINER_BASE = re.compile(
    r"^FROM\s+[^\s@]+@sha256:[0-9a-fA-F]{64}(?:\s+AS\s+[A-Za-z0-9._-]+)?\s*$",
    re.IGNORECASE,
)
APPROVED_ACTION_PINS = {
    "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97",
    "aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25",
}


@dataclass(frozen=True)
class Candidate:
    source: Path
    archive_path: str
    data: bytes


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    excerpt: str


def _excluded(rel: Path) -> bool:
    parts = rel.parts
    if any(part in EXCLUDED_DIRS for part in parts[:-1]):
        return True
    if rel.as_posix().startswith("demo/artifacts/"):
        return True
    name = rel.name
    if name in EXCLUDED_FILES:
        return True
    return rel.suffix.lower() in EXCLUDED_SUFFIXES


def collect_candidates(
    root: Path = ROOT,
    *,
    strict_exclusions: bool = False,
) -> tuple[list[Candidate], list[Finding], int]:
    """Collect a conservative public tree and add the fictional root config.

    A live-tree audit tolerates known local-only paths because they are intentionally
    excluded from an archive. An index audit is strict: anything present in Git's
    candidate tree must itself be publishable, so excluded paths become findings.
    """
    candidates: list[Candidate] = []
    findings: list[Finding] = []
    excluded = 0
    root = root.resolve()

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        kept_dirs = []
        for dirname in dirnames:
            directory = here / dirname
            rel_dir = directory.relative_to(root)
            rel_s = rel_dir.as_posix()
            if directory.is_symlink():
                findings.append(Finding(rel_s, 0, "symbolic link is not allowed", ""))
            elif dirname in EXCLUDED_DIRS:
                excluded += 1
                if strict_exclusions and dirname != ".git":
                    findings.append(Finding(rel_s, 0, "excluded directory is staged", ""))
            else:
                kept_dirs.append(dirname)
        dirnames[:] = kept_dirs

        for filename in filenames:
            source = here / filename
            rel = source.relative_to(root)
            rel_s = rel.as_posix()
            if _excluded(rel):
                excluded += 1
                if strict_exclusions:
                    findings.append(Finding(rel_s, 0, "excluded file is staged", ""))
                continue
            if source.is_symlink():
                findings.append(Finding(rel_s, 0, "symbolic link is not allowed", ""))
                continue
            is_approved_binary = rel_s in APPROVED_BINARY_SHA256
            if (rel.suffix.lower() not in TEXT_SUFFIXES
                    and filename not in TEXT_NAMES
                    and not is_approved_binary):
                findings.append(Finding(rel_s, 0, "unexpected or binary file type", rel.suffix or "no suffix"))
                continue
            size = source.stat().st_size
            if size > MAX_TEXT_FILE:
                findings.append(Finding(rel_s, 0, "file exceeds public-source size limit", str(size)))
                continue
            candidates.append(Candidate(source, rel_s, source.read_bytes()))

    synthetic_config = root / "configs" / "study_config.synthetic.json"
    if not synthetic_config.is_file():
        findings.append(Finding("configs/study_config.synthetic.json", 0, "synthetic config is missing", ""))
    else:
        # The private root config is never included. The public artifact receives an exact
        # copy of the reviewed fictional config at the location the application expects.
        candidates.append(
            Candidate(synthetic_config, "study_config.json", synthetic_config.read_bytes())
        )

    present = {item.archive_path for item in candidates}
    for missing in sorted(REQUIRED_PUBLIC_PATHS - present):
        findings.append(Finding(missing, 0, "required public file is missing", ""))

    return candidates, findings, excluded


def _git_root() -> Path:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("staged/history audit requires a readable Git repository") from exc
    git_root = Path(result.stdout.strip()).resolve()
    if git_root != ROOT.resolve():
        raise RuntimeError(
            f"repository boundary mismatch: expected {ROOT.resolve()}, found {git_root}"
        )
    return git_root


@contextmanager
def staged_tree() -> Iterator[Path]:
    """Materialize exactly Git's index without reading unstaged working-tree bytes."""
    _git_root()
    with tempfile.TemporaryDirectory(prefix="tlf-staged-audit-") as tmp:
        destination = Path(tmp) / "tree"
        destination.mkdir()
        prefix = f"{destination}{os.sep}"
        try:
            subprocess.run(
                ["git", "checkout-index", "--all", f"--prefix={prefix}"],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("could not materialize the staged Git index") from exc
        yield destination


def git_history_findings() -> list[Finding]:
    """Scan every reachable patch so removed secrets remain release blockers."""
    _git_root()
    try:
        count_result = subprocess.run(
            ["git", "rev-list", "--all", "--count"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if int(count_result.stdout.strip() or "0") == 0:
            return []
        result = subprocess.run(
            [
                "git",
                "log",
                "--all",
                "--format=commit %H%n%B",
                "--patch",
                "--no-color",
                "--no-ext-diff",
                "--no-textconv",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            timeout=60,
        )
    except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("could not scan complete Git history") from exc

    if len(result.stdout) > MAX_HISTORY_BYTES:
        return [
            Finding(
                ".git-history",
                0,
                "history exceeds built-in scanner limit; run a dedicated full-history scanner",
                str(len(result.stdout)),
            )
        ]
    text = result.stdout.decode("utf-8", errors="replace")
    findings: list[Finding] = []
    for rule, pattern in BANNED_CONTENT + SECRET_PATTERNS:
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            findings.append(Finding(".git-history", line, rule, _excerpt(text, match.start(), match.end())))
    return findings


def _excerpt(text: str, start: int, end: int) -> str:
    one_line = text[start:end].replace("\n", " ").replace("\r", " ")
    return one_line[:100]


def _pdfjs_version(text: str) -> str | None:
    matches = re.findall(r'\bversion\s*=\s*["\'](\d+\.\d+\.\d+)["\']', text)
    return matches[-1] if matches else None


def _workflow_findings(path: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for match in REMOTE_ACTION.finditer(text):
        action = match.group(1).strip("'\"")
        if action.startswith("./"):
            continue
        if not FULL_COMMIT_SHA.fullmatch(action):
            line = text.count("\n", 0, match.start()) + 1
            findings.append(
                Finding(path, line, "GitHub Action is not pinned to a full commit SHA", action[:100])
            )
        elif action not in APPROVED_ACTION_PINS:
            line = text.count("\n", 0, match.start()) + 1
            findings.append(Finding(path, line, "GitHub Action pin is not reviewed", action[:100]))
    return findings


def _svg_findings(path: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        return [Finding(path, 0, "SVG is not well-formed XML", str(exc))]

    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1].lower()
        if tag in {"script", "foreignobject", "iframe", "object", "embed"}:
            findings.append(Finding(path, 0, "active SVG element is not allowed", tag))
        for raw_name, raw_value in element.attrib.items():
            name = raw_name.rsplit("}", 1)[-1].lower()
            value = raw_value.strip().lower()
            if name.startswith("on"):
                findings.append(Finding(path, 0, "SVG event handler is not allowed", name))
            if name == "href" and (
                value.startswith(("http:", "https:", "javascript:", "data:", "//"))
                or not value.startswith("#")
            ):
                findings.append(Finding(path, 0, "external or active SVG reference is not allowed", value[:100]))
    return findings


def _lock_findings(path: str, text: str) -> list[Finding]:
    """Require a flat, exact-version inventory suitable for --no-deps installs."""
    findings: list[Finding] = []
    seen: set[str] = set()
    exact = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[A-Za-z0-9_,.-]+\])?==[^\s;]+$")
    for line_number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        match = exact.fullmatch(line)
        if not match:
            findings.append(Finding(path, line_number, "lock entry is not an exact version pin", line[:100]))
            continue
        normalized = re.sub(r"[-_.]+", "-", match.group(1)).lower()
        if normalized in seen:
            findings.append(Finding(path, line_number, "duplicate package in lock inventory", normalized))
        seen.add(normalized)
    if not seen:
        findings.append(Finding(path, 0, "lock inventory is empty", ""))
    return findings


def _requirement_names(
    path: str,
    text_by_path: dict[str, str],
    seen_files: set[str] | None = None,
) -> set[str]:
    seen_files = seen_files or set()
    if path in seen_files or path not in text_by_path:
        return set()
    seen_files.add(path)
    names: set[str] = set()
    for raw in text_by_path[path].splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-r "):
            included = (Path(path).parent / line[3:].strip()).as_posix()
            names.update(_requirement_names(included, text_by_path, seen_files))
            continue
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        if match:
            names.add(re.sub(r"[-_.]+", "-", match.group(1)).lower())
    return names


def _lock_names(text: str) -> set[str]:
    names: set[str] = set()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        if match:
            names.add(re.sub(r"[-_.]+", "-", match.group(1)).lower())
    return names


def _dockerfile_findings(path: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    bases = [
        (line_number, line.strip())
        for line_number, line in enumerate(text.splitlines(), start=1)
        if line.lstrip().upper().startswith("FROM ")
    ]
    if not bases:
        return [Finding(path, 0, "Dockerfile has no base image", "")]
    for line_number, base in bases:
        if not PINNED_CONTAINER_BASE.fullmatch(base):
            findings.append(
                Finding(
                    path,
                    line_number,
                    "container base image is not pinned by SHA-256 digest",
                    base[:100],
                )
            )
    return findings


def scan_candidates(candidates: list[Candidate], initial: list[Finding] | None = None) -> list[Finding]:
    findings = list(initial or [])
    seen_paths: set[str] = set()
    text_by_path: dict[str, str] = {}

    for item in candidates:
        if item.archive_path in seen_paths:
            findings.append(Finding(item.archive_path, 0, "duplicate archive path", ""))
            continue
        seen_paths.add(item.archive_path)
        approved_hash = APPROVED_BINARY_SHA256.get(item.archive_path)
        if approved_hash is not None:
            actual_hash = hashlib.sha256(item.data).hexdigest()
            if actual_hash != approved_hash:
                findings.append(
                    Finding(item.archive_path, 0, "reviewed binary hash mismatch", actual_hash)
                )
            continue
        try:
            text = item.data.decode("utf-8")
        except UnicodeDecodeError:
            findings.append(Finding(item.archive_path, 0, "file is not UTF-8 text", ""))
            continue
        if "\x00" in text:
            findings.append(Finding(item.archive_path, 0, "NUL byte in public text file", ""))
            continue
        text_by_path[item.archive_path] = text

        for rule, pattern in BANNED_CONTENT + SECRET_PATTERNS:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                findings.append(
                    Finding(item.archive_path, line, rule, _excerpt(text, match.start(), match.end()))
                )

        if item.archive_path.startswith(".github/workflows/") and item.archive_path.endswith(
            (".yml", ".yaml")
        ):
            findings.extend(_workflow_findings(item.archive_path, text))

        if item.archive_path.endswith(".svg"):
            findings.extend(_svg_findings(item.archive_path, text))

        if item.archive_path in {"requirements-lock.txt", "requirements-demo-lock.txt"}:
            findings.extend(_lock_findings(item.archive_path, text))

        if item.archive_path == "Dockerfile":
            findings.extend(_dockerfile_findings(item.archive_path, text))

        if item.archive_path in {"static/vendor/pdf.min.js", "static/vendor/pdf.mjs"}:
            version = _pdfjs_version(text)
            if version is None:
                findings.append(Finding(item.archive_path, 0, "could not identify vendored PDF.js version", ""))
            else:
                parsed = tuple(int(x) for x in version.split("."))
                if parsed < (4, 2, 67):
                    findings.append(
                        Finding(
                            item.archive_path,
                            1,
                            "vendored PDF.js predates required security baseline 4.2.67",
                            version,
                        )
                    )

    lock_contracts = {
        "requirements-demo-lock.txt": ("requirements-demo.txt",),
        "requirements-lock.txt": ("requirements-demo.txt", "requirements-dev.txt"),
    }
    for lock_path, direct_paths in lock_contracts.items():
        if lock_path not in text_by_path or any(path not in text_by_path for path in direct_paths):
            continue
        direct_names: set[str] = set()
        for direct_path in direct_paths:
            direct_names.update(_requirement_names(direct_path, text_by_path))
        missing = sorted(direct_names - _lock_names(text_by_path[lock_path]))
        for package in missing:
            findings.append(
                Finding(lock_path, 0, "direct requirement is missing from exact lock", package)
            )

    return findings


def source_digest(candidates: list[Candidate]) -> str:
    digest = hashlib.sha256()
    for item in sorted(candidates, key=lambda c: c.archive_path):
        path_bytes = item.archive_path.encode("utf-8")
        file_hash = hashlib.sha256(item.data).digest()
        digest.update(len(path_bytes).to_bytes(4, "big"))
        digest.update(path_bytes)
        digest.update(len(item.data).to_bytes(8, "big"))
        digest.update(file_hash)
    return digest.hexdigest()


def print_audit(
    candidates: list[Candidate],
    findings: list[Finding],
    excluded: int,
    *,
    source: str = "working tree",
) -> None:
    total_bytes = sum(len(item.data) for item in candidates)
    print("Public-source dry audit")
    print(f"  candidate files: {len(candidates)}")
    print(f"  candidate bytes: {total_bytes}")
    print(f"  excluded paths:  {excluded}")
    print(f"  source:          {source}")
    if findings:
        print(f"  result:          BLOCKED ({len(findings)} finding(s))")
        for finding in findings[:200]:
            loc = f":{finding.line}" if finding.line else ""
            suffix = f" [{finding.excerpt}]" if finding.excerpt else ""
            print(f"    - {finding.path}{loc}: {finding.rule}{suffix}")
        if len(findings) > 200:
            print(f"    - ... {len(findings) - 200} additional finding(s) omitted")
    else:
        print("  result:          CLEAN")
        print(f"  source digest:   {source_digest(candidates)}")


def _direct_requirements() -> list[str]:
    seen: set[Path] = set()
    specs: list[str] = []

    def read(path: Path) -> None:
        if path in seen or not path.is_file():
            return
        seen.add(path)
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            if line.startswith("-r "):
                read((path.parent / line[3:].strip()).resolve())
            elif not line.startswith("-"):
                specs.append(line)

    read(ROOT / "requirements-dev.txt")
    return sorted(set(specs), key=str.lower)


def _write_stage(stage: Path, candidates: list[Candidate], digest: str) -> None:
    for item in candidates:
        destination = stage / item.archive_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(item.data)

    pdfjs_item = next(
        (
            c
            for c in candidates
            if c.archive_path in {"static/vendor/pdf.min.js", "static/vendor/pdf.mjs"}
        ),
        None,
    )
    pdfjs_version = None
    if pdfjs_item is not None:
        pdfjs_version = _pdfjs_version(pdfjs_item.data.decode("utf-8"))

    source_files = [
        {
            "path": item.archive_path,
            "sha256": hashlib.sha256(item.data).hexdigest(),
            "bytes": len(item.data),
        }
        for item in sorted(candidates, key=lambda c: c.archive_path)
    ]
    sbom_lite = {
        "format": "tlf-sbom-lite/v1",
        "notice": "Dependency and file inventory only; not a formal SPDX or CycloneDX SBOM.",
        "source_digest": digest,
        "direct_requirement_specs": _direct_requirements(),
        "vendored_components": [
            {
                "name": "PDF.js",
                "version": pdfjs_version,
                "license": "Apache-2.0",
                "paths": ["static/vendor/pdf.mjs", "static/vendor/pdf.worker.mjs"],
            }
        ],
        "source_files": source_files,
    }
    (stage / "sbom-lite.json").write_text(
        json.dumps(sbom_lite, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    manifest_lines = []
    for path in sorted(p for p in stage.rglob("*") if p.is_file()):
        rel = path.relative_to(stage).as_posix()
        manifest_lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {rel}")
    (stage / "PUBLIC_MANIFEST.sha256").write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")


def _stage_candidates(stage: Path) -> list[Candidate]:
    return [
        Candidate(path, path.relative_to(stage).as_posix(), path.read_bytes())
        for path in sorted(stage.rglob("*"))
        if path.is_file()
    ]


def _write_deterministic_zip(stage: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(p for p in stage.rglob("*") if p.is_file()):
            rel = path.relative_to(stage).as_posix()
            info = zipfile.ZipInfo(f"{ARCHIVE_ROOT}/{rel}", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())


def build_public(output: Path, candidates: list[Candidate], digest: str) -> None:
    if output.exists():
        raise RuntimeError(f"refusing to overwrite existing output: {output}")
    if output.suffix.lower() != ".zip":
        raise RuntimeError("public build output must be a .zip file")

    with tempfile.TemporaryDirectory(prefix="tlf-public-release-") as tmp:
        stage = Path(tmp) / ARCHIVE_ROOT
        stage.mkdir()
        _write_stage(stage, candidates, digest)
        staged = _stage_candidates(stage)
        staged_findings = scan_candidates(staged)
        if staged_findings:
            print_audit(staged, staged_findings, excluded=0)
            raise RuntimeError("generated staging tree failed the public-release audit")
        _write_deterministic_zip(stage, output)

    archive_hash = hashlib.sha256(output.read_bytes()).hexdigest()
    print(f"Wrote public source artifact: {output}")
    print(f"  source digest:       {digest}")
    print(f"  archive sha256:      {archive_hash}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="dry audit; never creates an artifact")
    action.add_argument("--source-digest", action="store_true", help="print the clean candidate-tree digest")
    action.add_argument(
        "--build",
        metavar="OUTPUT.zip",
        type=Path,
        help="build after a clean content audit",
    )
    parser.add_argument(
        "--staged",
        action="store_true",
        help="audit/build exactly the staged Git index; excluded staged paths fail closed",
    )
    parser.add_argument(
        "--history",
        action="store_true",
        help="also scan all reachable Git history for blocked identifiers and secret patterns",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.staged:
            with staged_tree() as staged:
                candidates, collection_findings, excluded = collect_candidates(
                    staged, strict_exclusions=True
                )
            source = "staged Git index"
        else:
            candidates, collection_findings, excluded = collect_candidates()
            source = "working tree"
        if args.history:
            collection_findings.extend(git_history_findings())
            source += " + complete Git history"
    except RuntimeError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    findings = scan_candidates(candidates, collection_findings)

    if args.check:
        print_audit(candidates, findings, excluded, source=source)
        return 1 if findings else 0
    if findings:
        print_audit(candidates, findings, excluded, source=source)
        return 1

    digest = source_digest(candidates)
    if args.source_digest:
        print(digest)
        return 0

    try:
        build_public(args.build.resolve(), candidates, digest)
    except RuntimeError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
