"""Main-document selection: the user-marked edition is the one reviewed.

At creation the reviewer may mark one uploaded PDF as the main (reviewed) edition; it is
stored role='delivery' and the rest role='prior' (comparison). runner._pick_current_prior
must then return the marked doc as `current` regardless of edition year, and the
highest-edition comparison doc as `prior`. With no explicit pick (every doc role='delivery',
e.g. a single upload) it falls back to the legacy highest-edition-wins behaviour.
Self-contained fixture so this file is byte-identical across both editions.
"""

import pytest

import db
import runner


@pytest.fixture()
def iso_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "app.db"))
    if hasattr(db._local, "conn"):
        del db._local.conn
    db.init()
    yield tmp_path
    if hasattr(db._local, "conn"):
        del db._local.conn


def _project(name="p"):
    return db.insert("project", compound="C", study="S", name=name, edition_label="",
                     created_at=db.now_iso())


def _doc(pid, role, edition, filename="d.pdf"):
    return db.insert("document", project_id=pid, role=role, filename=filename,
                     path=filename, n_pages=3, edition=edition)


def test_explicit_main_overrides_edition(iso_db):
    # User marked the older edition as main; the newer one is comparison only.
    pid = _project()
    main = _doc(pid, "delivery", "2024", "annual_2024.pdf")
    comp = _doc(pid, "prior", "2025", "annual_2025.pdf")
    current, prior = runner._pick_current_prior(pid)
    assert current["id"] == main
    assert prior["id"] == comp


def test_auto_pick_highest_edition_when_no_explicit_main(iso_db):
    # No explicit pick (both role='delivery') → highest edition is reviewed, as before.
    pid = _project()
    older = _doc(pid, "delivery", "2024", "annual_2024.pdf")
    newer = _doc(pid, "delivery", "2025", "annual_2025.pdf")
    current, prior = runner._pick_current_prior(pid)
    assert current["id"] == newer
    assert prior["id"] == older


def test_single_document_has_no_prior(iso_db):
    pid = _project()
    only = _doc(pid, "delivery", "2025")
    current, prior = runner._pick_current_prior(pid)
    assert current["id"] == only
    assert prior is None


def test_multiple_priors_pick_highest_edition(iso_db):
    # With several comparison docs, the highest-edition prior is chosen.
    pid = _project()
    main = _doc(pid, "delivery", "2025", "annual_2025.pdf")
    _doc(pid, "prior", "2023", "annual_2023.pdf")
    p24 = _doc(pid, "prior", "2024", "annual_2024.pdf")
    current, prior = runner._pick_current_prior(pid)
    assert current["id"] == main
    assert prior["id"] == p24
