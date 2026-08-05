from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import yaml

from bento_converter.errors import BentoConverterError
from bento_converter.html_import import normalize_imported_html
from scripts.deck_workflow import command_migrate, load_state
from scripts.import_html_deck import run as run_import


ROOT = Path(__file__).resolve().parents[1]


class HtmlImportNormalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "imports").mkdir()
        (self.root / "deck").mkdir()
        self.input = self.root / "imports/original.html"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def normalize(self, source: str, **overrides):
        self.input.write_text(source, encoding="utf-8")
        options = {
            "slide_selector": ".page", "width": 1280, "height": 720,
            "copy_assets": False, "generate_ids": True,
        }
        options.update(overrides)
        return normalize_imported_html(
            source, input_path=self.input, repository=self.root, **options,
        )

    def test_static_sanitization_disables_active_and_remote_content(self) -> None:
        source = """<!doctype html><html><head>
<script>fetch('https://example.com')</script>
<style>@import 'https://example.com/x.css'; .x{filter:blur(2px);background:url(https://example.com/a.png)}</style>
</head><body><div class="page" onclick="evil()"><h1>Title</h1>
<img src="https://example.com/image.png"><iframe src="https://example.com/frame"></iframe></div></body></html>"""
        normalized, registry, assets, report = self.normalize(source)
        self.assertNotIn("<script", normalized)
        self.assertNotIn("onclick", normalized)
        self.assertNotIn("<iframe", normalized)
        self.assertNotIn('<img src="https://example.com/image.png"', normalized)
        self.assertIn("data-import-remote-src", normalized)
        self.assertIn("width:1280px;height:720px", normalized)
        self.assertIn("data-slide-id=", normalized)
        self.assertIn("data-bento-id=", normalized)
        self.assertTrue(report["removedEventHandlers"])
        self.assertGreaterEqual(len(report["remoteResources"]), 2)
        self.assertTrue(report["unsupportedCss"])
        self.assertEqual(report["scriptExecution"], "disabled-static-parser")
        self.assertEqual(registry["sources"]["imported-html"]["role"], "imported")
        self.assertEqual(assets, {})

    def test_javascript_url_and_ambiguous_selector_are_rejected(self) -> None:
        with self.assertRaisesRegex(BentoConverterError, "javascript"):
            self.normalize('<div class="page"><a href="javascript:alert(1)">x</a></div>')
        with self.assertRaisesRegex(BentoConverterError, "ambiguous"):
            self.normalize("<main><article>one</article><article>two</article></main>", slide_selector=None)

    def test_local_asset_copy_is_prepared_without_mutating_original(self) -> None:
        asset = self.root / "imports/image.png"
        asset.write_bytes(b"png-fixture")
        source = '<section class="page"><img src="image.png"></section>'
        before = source.encode("utf-8")
        normalized, _, assets, report = self.normalize(source, copy_assets=True)
        self.assertEqual(self.input.read_bytes(), before)
        self.assertIn("assets/imported/", normalized)
        self.assertEqual(len(assets), 1)
        destination = Path(next(iter(assets)))
        self.assertEqual(assets[str(destination)], b"png-fixture")
        self.assertEqual(len(report["localResources"]), 1)


class HtmlImportCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for directory in ("workflow", "sources/private", "output", "imports"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        for relative in ("deck.yaml", "workflow/deck.schema.json", "workflow/deck.v1.schema.json"):
            shutil.copy2(ROOT / relative, self.root / relative)
        (self.root / "REQUEST.md").write_text("# Request\n", encoding="utf-8")
        command_migrate(self.root, load_state(self.root), dry_run=False, report_path=None)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_cli_installs_imported_mode_manifest_assets_and_report_atomically(self) -> None:
        asset = self.root / "imports/図.png"
        asset.write_bytes(b"asset")
        source = self.root / "imports/既存 資料.html"
        source.write_text(
            '<!doctype html><html><body><div class="page"><h1>Imported</h1><img src="図.png"></div></body></html>',
            encoding="utf-8",
        )
        original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        result = run_import(SimpleNamespace(
            root=self.root, input=source, slide_selector=".page", width=1280, height=720,
            copy_assets=True, generate_ids=True, force=False,
        ))
        self.assertEqual(result, 0)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original_hash)
        state = load_state(self.root)
        self.assertEqual(state["authoring"]["mode"], "imported")
        self.assertEqual(state["sources"]["authorityMode"], "imported")
        self.assertEqual(state["sections"]["imported-deck"]["status"], "authoring")
        manifest = yaml.safe_load((self.root / "sources/source-manifest.yaml").read_text(encoding="utf-8"))
        self.assertEqual(manifest["authorityMode"], "imported")
        self.assertEqual(manifest["items"][-1]["path"], "imports/既存 資料.html")
        self.assertTrue((self.root / "deck/deck.preview.html").is_file())
        self.assertTrue((self.root / "deck/deck.registry.json").is_file())
        self.assertTrue(list((self.root / "deck/assets/imported").glob("*-図.png")))
        report = json.loads((self.root / "output/import-report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "pass")
        journals = list((self.root / "output/.bento-transactions/archive").rglob("*.json"))
        self.assertTrue(any(json.loads(path.read_text(encoding="utf-8"))["operation"] == "import-html-deck" for path in journals))


if __name__ == "__main__":
    unittest.main()
