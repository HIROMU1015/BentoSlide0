from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bento_converter.errors import ValidationError
from bento_converter.html_pipeline import build_from_html
from bento_converter.html_preflight import (
    PREFLIGHT_FORMAT,
    analyze_html_sources,
    write_preflight_report,
)
from bento_converter.html_source import SourceUnit
from scripts.check_html_preflight import run as run_cli


def registry(*, protected: list[str] | None = None) -> dict:
    return {
        "format": "bento/html-registry/v2",
        "unitId": "deck",
        "sources": {},
        "document": {"title": "Preflight"},
        "assets": {}, "fonts": {}, "equations": {}, "figures": {}, "tables": {}, "charts": {},
        "protected": {"slideIds": [], "elementIds": protected or [], "requiredText": []},
    }


def write_pair(root: Path, html: str, *, protected: list[str] | None = None) -> tuple[Path, Path, dict]:
    html_path = root / "deck.preview.html"
    registry_path = root / "deck.registry.json"
    value = registry(protected=protected)
    html_path.write_text(html, encoding="utf-8")
    registry_path.write_text(json.dumps(value), encoding="utf-8")
    return html_path, registry_path, value


class HtmlPreflightTests(unittest.TestCase):
    def test_nested_text_shape_reports_decomposition_without_covering_protected_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            html_path, registry_path, value = write_pair(
                root,
                "<section class='slide' data-slide-id='one'>"
                "<div data-bento-id='card' data-bento-type='shape'>Card label"
                "<span data-bento-id='protected-label' data-bento-type='text'>Label</span>"
                "</div></section>",
                protected=["protected-label"],
            )
            report = analyze_html_sources([SourceUnit("deck", html_path, registry_path, value)], value)

            codes = [item.code for item in report.issues]
            self.assertIn("text-bearing-shape", codes)
            self.assertIn("nested-conversion-annotation", codes)
            self.assertNotIn("protected-element-covered", codes)
            self.assertTrue(report.passed)

    def test_explicit_whole_fallback_cannot_bake_editable_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            html_path, registry_path, value = write_pair(
                root,
                "<section class='slide' data-slide-id='one'>"
                "<div data-bento-id='card' data-bento-export='image' data-bento-fallback-scope='whole'>"
                "Label<span data-bento-id='child' data-bento-type='text'>Editable</span>"
                "</div></section>",
            )
            report = analyze_html_sources([SourceUnit("deck", html_path, registry_path, value)], value)
            self.assertFalse(report.passed)
            self.assertIn("whole-fallback-covers-editable-content", [item.code for item in report.errors])

    def test_separate_text_free_shape_and_text_pass_without_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            html_path, registry_path, value = write_pair(
                root,
                "<section class='slide' data-slide-id='one'>"
                "<div data-bento-id='background' data-bento-type='shape'></div>"
                "<p data-bento-id='label' data-bento-type='text'>Editable label</p>"
                "</section>",
            )
            report = analyze_html_sources([SourceUnit("deck", html_path, registry_path, value)], value)

            self.assertTrue(report.passed)
            self.assertEqual(report.issues, ())
            self.assertEqual(report.as_dict()["summary"]["annotatedElements"], 2)

    def test_unique_bento_parts_inside_one_owner_are_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            html_path, registry_path, value = write_pair(
                root,
                "<section class='slide' data-slide-id='one'>"
                "<div data-bento-id='card' data-bento-type='shape'>"
                "<strong data-bento-part='heading'>Heading</strong>"
                "<span data-bento-part='body'>Body</span>"
                "</div></section>",
            )
            report = analyze_html_sources([SourceUnit("deck", html_path, registry_path, value)], value)

            self.assertTrue(report.passed)
            self.assertNotIn("invalid-bento-part-id", [item.code for item in report.issues])
            self.assertNotIn("duplicate-bento-part-id", [item.code for item in report.issues])

    def test_duplicate_or_unowned_bento_parts_are_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            html_path, registry_path, value = write_pair(
                root,
                "<section class='slide' data-slide-id='one'>"
                "<span data-bento-part='orphan'>Orphan</span>"
                "<div data-bento-id='card' data-bento-type='shape'>"
                "<strong data-bento-part='same'>Heading</strong>"
                "<span data-bento-part='same'>Body</span>"
                "</div></section>",
            )
            report = analyze_html_sources([SourceUnit("deck", html_path, registry_path, value)], value)

            self.assertFalse(report.passed)
            codes = [item.code for item in report.errors]
            self.assertIn("bento-part-without-owner", codes)
            self.assertIn("duplicate-bento-part-id", codes)

    def test_duplicate_ids_and_fallback_without_id_fail_before_browser_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            html_path, registry_path, _ = write_pair(
                root,
                "<section class='slide' data-slide-id='one'>"
                "<div data-bento-id='duplicate'></div><p data-bento-id='duplicate'>Again</p>"
                "<div data-bento-export='image'>Visual</div>"
                "</section>",
            )
            output = root / "output/presentation.bento.html"
            with patch("bento_converter.html_pipeline.BrowserHarness") as browser:
                with self.assertRaises(ValidationError):
                    build_from_html(
                        html_path=html_path,
                        registry_path=registry_path,
                        base_path=root / "base-does-not-need-to-exist.html",
                        output_path=output,
                    )
                browser.assert_not_called()

            payload = json.loads((root / "output/diagnostics/html-preflight.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["format"], PREFLIGHT_FORMAT)
            self.assertFalse(payload["passed"])
            self.assertEqual(payload["summary"]["errors"], 2)

    def test_cli_writes_deterministic_report_and_strict_mode_rejects_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            html_path, registry_path, _ = write_pair(
                root,
                "<section class='slide' data-slide-id='one'>"
                "<div data-bento-id='card' data-bento-type='shape'>Label</div>"
                "</section>",
            )
            report_path = root / "preflight.json"
            args = argparse.Namespace(
                html=html_path, registry=registry_path, html_dir=None, registry_dir=None,
                report=report_path, strict_editability=True, verbose=False,
            )
            self.assertEqual(run_cli(args), 3)
            first = report_path.read_bytes()
            self.assertEqual(run_cli(args), 3)
            self.assertEqual(report_path.read_bytes(), first)


if __name__ == "__main__":
    unittest.main()
