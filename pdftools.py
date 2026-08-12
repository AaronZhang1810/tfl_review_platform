"""PDF utilities: clip page ranges into single-output PDFs, and extract text.

Rendering happens client-side in pdf.js, so the server only needs to (a) produce a clipped PDF for one output's page range and (b) pull text for the AI/hashing."""

from __future__ import annotations

import hashlib
import io
import os
import threading

import pypdf
import pdfplumber

# file_hash() results, keyed by (abspath, size, mtime_ns). A delivery is hashed once per run instead of once per output, and the key self-invalidates: a file whose size or mtime changed gets a fresh entry, so a stale digest can never be served.
_FILE_HASH_CACHE: dict[tuple, str] = {}
_fh_lock = threading.Lock()


def validate_pdf(path: str, max_pages: int) -> int:
    """Check the bounded structural properties required by this application.

This rejects mislabeled files, encrypted documents, empty PDFs, and page-count bombs before the indexer or renderer sees them.  It is a parser boundary check, not a claim that arbitrary PDF content is safe."""
    try:
        with open(path, "rb") as fh:
            header = fh.read(1024)
        if b"%PDF-" not in header:
            raise ValueError("file does not have a PDF header")
        reader = pypdf.PdfReader(path, strict=False)
        if reader.is_encrypted:
            raise ValueError("encrypted PDFs are not supported")
        pages = len(reader.pages)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("file is not a readable PDF") from exc
    if pages < 1:
        raise ValueError("PDF has no pages")
    if pages > max_pages:
        raise ValueError(f"PDF exceeds the configured {max_pages}-page limit")
    return pages


def file_hash(path: str) -> str:
    """Digest of the RAW FILE BYTES — the cheap, exact "has this PDF changed?" signal.

Reading a delivery's text to hash it costs ~0.6 s/page (pdfplumber); hashing the file itself costs ~0.02 s for 12 MB. So this is what lets a run decide it can reuse a stored extraction without opening the PDF at all. Byte-exact, unlike size+mtime."""
    st = os.stat(path)
    key = (os.path.abspath(path), st.st_size, st.st_mtime_ns)
    with _fh_lock:
        cached = _FILE_HASH_CACHE.get(key)
    if cached:
        return cached
    d = hashlib.blake2b(digest_size=16)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            d.update(chunk)
    digest = d.hexdigest()
    with _fh_lock:
        _FILE_HASH_CACHE[key] = digest
    return digest


def page_count(path: str) -> int:
    return len(pypdf.PdfReader(path).pages)


def clip(path: str, page_start: int, page_end: int) -> bytes:
    """Return a PDF containing pages [page_start, page_end] (1-based inclusive)."""
    reader = pypdf.PdfReader(path)
    writer = pypdf.PdfWriter()
    lo = max(1, page_start)
    hi = min(len(reader.pages), page_end)
    for i in range(lo - 1, hi):
        writer.add_page(reader.pages[i])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def page_texts(path: str, page_start: int, page_end: int) -> list[str]:
    """Extract text for each page in the range (1-based inclusive)."""
    out: list[str] = []
    with pdfplumber.open(path) as pdf:
        lo = max(1, page_start)
        hi = min(len(pdf.pages), page_end)
        for i in range(lo - 1, hi):
            out.append(pdf.pages[i].extract_text() or "")
    return out


def range_text(path: str, page_start: int, page_end: int, max_chars: int = 60000) -> str:
    """Joined text for a page range, capped so a huge table can't blow the prompt."""
    joined = "\n".join(page_texts(path, page_start, page_end))
    return joined[:max_chars]


def page_top_texts(path: str, max_lines: int = 12) -> list[str]:
    """Top-of-page text (first ``max_lines`` lines) for each page, for caption detection.

Uses pypdfium2 (C-backed) so a many-hundred-page delivery is scanned in seconds: the full pdfplumber text pass is ~90× slower and we only need the caption printed at the top of each page. Falls back to pdfplumber if pypdfium2 is unavailable. Returns one string per page in document order."""
    try:
        import pypdfium2 as pdfium
    except Exception:
        return ["\n".join((t or "").splitlines()[:max_lines])
                for t in page_texts(path, 1, page_count(path))]
    out: list[str] = []
    pdf = pdfium.PdfDocument(path)
    try:
        for i in range(len(pdf)):
            page = pdf[i]
            tp = page.get_textpage()
            txt = tp.get_text_range()
            tp.close()
            page.close()
            out.append("\n".join((txt or "").splitlines()[:max_lines]))
    finally:
        pdf.close()
    return out


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:16]


def page_char_counts(path: str) -> list[int]:
    """Fast per-page character count via pypdfium2 (C-backed) — used for blank-page detection without the cost of full text extraction. Falls back to [] on error."""
    try:
        import pypdfium2 as pdfium
    except Exception:
        return []
    counts: list[int] = []
    pdf = pdfium.PdfDocument(path)
    try:
        for i in range(len(pdf)):
            page = pdf[i]
            tp = page.get_textpage()
            counts.append(max(0, tp.count_chars()))
            tp.close()
            page.close()
    finally:
        pdf.close()
    return counts
