import base64
import hashlib
from html.parser import HTMLParser
from pathlib import Path


APP_JS = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")


class _InlineScriptParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self._inline = False
        self._parts = []
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and "src" not in dict(attrs):
            self._inline = True
            self._parts = []

    def handle_data(self, data):
        if self._inline:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._inline:
            self.scripts.append("".join(self._parts))
            self._inline = False


def test_dynamic_statuses_and_comment_numbers_use_safe_rendering_helpers():
    assert "state-${f.state}" not in APP_JS
    assert "#${c.num" not in APP_JS
    assert "#${r.num" not in APP_JS
    assert "findingStateClass(f.state)" in APP_JS
    assert "displayInt(c.num)" in APP_JS
    assert "displayInt(r.num)" in APP_JS
    assert "STATUS_CLASSES[s]" in APP_JS


def test_persisted_import_fields_are_encoded_before_html_rendering():
    # These values can originate in a shared project bundle or edited workbook. Keep the assertions close to the dangerous template sinks so a later UI refactor cannot accidentally remove the output-encoding boundary.
    for safe_expression in (
        "${esc(p.name)}",
        "${esc(o.status)}",
        "${esc(o.label)}",
        "cell: o => esc(o.title)",
        "cell: o => esc(o.doc_filename || \"\")",
        "${esc(f.check_id)}",
        "${esc(f.message)}",
        "${esc(c.body)}",
        "${esc(r.body)}",
    ):
        assert safe_expression in APP_JS
    assert "replace(/[&<>\"']/g" in APP_JS


def test_csp_hashes_are_bound_to_every_inline_script():
    import main

    root = Path(__file__).resolve().parents[1]
    scripts = []
    for relative in ("static/index.html", "static/tutorial.html"):
        parser = _InlineScriptParser()
        parser.feed((root / relative).read_text(encoding="utf-8"))
        scripts.extend(parser.scripts)
    actual = {
        "'sha256-" + base64.b64encode(hashlib.sha256(script.encode()).digest()).decode() + "'"
        for script in scripts
    }
    assert actual == set(main.INLINE_SCRIPT_HASHES)
