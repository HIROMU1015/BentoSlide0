"""Tests for the isolated converter defect-repair loop."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bento_converter.conversion_repair import (
    DIAGNOSTICS_FORMAT,
    create_single_slide_reproducer,
    diagnostics_from_exception,
    select_focused_tests,
)
from bento_converter.errors import ValidationError, issue
from bento_converter.html_pipeline import HtmlBuildResult
from bento_converter.incremental_cache import (
    BENTO_RENDER_IMPLEMENTATION_FILES,
    SOURCE_LAYOUT_IMPLEMENTATION_FILES,
    VISUAL_COMPARISON_IMPLEMENTATION_FILES,
)
from scripts.run_conversion_repair import run_repair


ROOT = Path(__file__).resolve().parents[1]


def registry_value() -> dict:
    return {
        "format": "bento/html-registry/v2",
        "unitId": "deck",
        "sources": {},
        "document": {"title": "Repair fixture"},
        "assets": {},
        "fonts": {},
        "equations": {},
        "figures": {},
        "tables": {},
        "charts": {},
        "protected": {"slideIds": [], "elementIds": [], "requiredText": []},
    }


def write_repair_repository(root: Path, *, real_base: bool = False) -> dict:
    (root / "deck").mkdir(parents=True)
    (root / "deck/deck.preview.html").write_text(
        "<!doctype html><html><head><style>"
        "body{margin:0}.slide{position:relative;width:1280px;height:720px;background:#fff}"
        ".block{position:absolute;left:180px;top:160px;width:920px;height:400px}"
        "</style></head><body>"
        "<section class='slide' data-slide-id='one' data-section-id='intro'>"
        "<div class='block' data-bento-id='one-title' data-bento-type='shape' data-bento-shape='rect' "
        "style='background:#2563eb'></div></section>"
        "<section class='slide' data-slide-id='two' data-section-id='body'>"
        "<div class='block' data-bento-id='two-title' data-bento-type='shape' data-bento-shape='rect' "
        "style='background:#7c3aed'></div></section>"
        "</body></html>",
        encoding="utf-8",
    )
    (root / "deck/deck.registry.json").write_text(
        json.dumps(registry_value(), ensure_ascii=False), encoding="utf-8",
    )
    if real_base:
        shutil.copy2(ROOT / "Bento_Slides.base.bento.html", root / "Bento_Slides.base.bento.html")
    else:
        (root / "Bento_Slides.base.bento.html").write_text("base", encoding="utf-8")
    return {
        "authoring": {
            "mode": "single",
            "entryHtml": "deck/deck.preview.html",
            "registry": "deck/deck.registry.json",
        }
    }


def args_for(root: Path, *, changed_paths: list[str] | None = None, run_tests: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        root=root,
        workspace=None,
        browser_executable=None,
        changed_path=changed_paths or [],
        run_focused_tests=run_tests,
    )


class ConversionRepairUnitTests(unittest.TestCase):
    def test_validation_errors_become_separate_contextual_diagnostics(self) -> None:
        error = ValidationError([
            issue(slide_id="one", element_id="title", field="x", actual=-1, fix="Keep x non-negative."),
            issue(slide_id="two", element_id="shape", field="type", actual="mystery", fix="Use a supported type."),
        ])

        diagnostics = diagnostics_from_exception(error, phase="converting")

        self.assertEqual(len(diagnostics), 2)
        self.assertEqual(diagnostics[0].slide_id, "one")
        self.assertEqual(diagnostics[0].element_id, "title")
        self.assertEqual(diagnostics[1].field, "type")

    def test_test_selection_combines_failure_phase_and_changed_components_once(self) -> None:
        diagnostic = diagnostics_from_exception(
            ValidationError([issue(slide_id="one", field="type", actual="bad", fix="Fix it.")]),
            phase="converting",
        )
        selected = select_focused_tests(
            diagnostic,
            changed_paths=["bento_converter/html_converter.py", "bento_converter/browser_check.py"],
        )
        keys = [(item.test_id, item.browser) for item in selected]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertIn(("tests.test_html_first.HtmlConversionTests", False), keys)
        self.assertIn(("tests.test_browser.BrowserIntegrationTests", True), keys)

    def test_phase_specific_cache_digests_do_not_invalidate_source_for_converter_fix(self) -> None:
        self.assertNotIn("html_converter.py", SOURCE_LAYOUT_IMPLEMENTATION_FILES)
        self.assertNotIn("browser_check.py", SOURCE_LAYOUT_IMPLEMENTATION_FILES)
        self.assertIn("browser_check.py", BENTO_RENDER_IMPLEMENTATION_FILES)
        self.assertIn("visual_comparison.py", VISUAL_COMPARISON_IMPLEMENTATION_FILES)

    def test_single_slide_reproducer_preserves_global_html_and_removes_other_slides(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_repair_repository(root)
            destination = root / "output/reproducer"

            manifest = create_single_slide_reproducer(
                repository=root,
                html_path=root / "deck/deck.preview.html",
                registry_path=root / "deck/deck.registry.json",
                base_path=root / "Bento_Slides.base.bento.html",
                slide_id="two",
                destination=destination,
            )

            reproduced = (destination / "deck/deck.preview.html").read_text(encoding="utf-8")
            self.assertIn(".slide{position:relative", reproduced)
            self.assertIn("data-slide-id='two'", reproduced)
            self.assertNotIn("data-slide-id='one'", reproduced)
            self.assertEqual(manifest["slideId"], "two")
            self.assertTrue((destination / "reproducer.json").is_file())

    def test_failed_repair_persists_all_diagnostics_and_reproducer_without_state_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = write_repair_repository(root)

            def failing_build(**values):
                values["progress"]("extracting-layout", {"slideCount": 2})
                raise ValidationError([
                    issue(slide_id="one", element_id="one-title", field="width", actual=-1, fix="Use a positive width."),
                    issue(slide_id="two", element_id="two-title", field="height", actual=-1, fix="Use a positive height."),
                ])

            with patch("scripts.run_conversion_repair.load_state", return_value=state):
                result = run_repair(args_for(root), build=failing_build)

            self.assertEqual(result, 2)
            status = json.loads((root / "output/conversion-repair/status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "failed")
            self.assertEqual(status["phase"], "extracting-layout")
            self.assertEqual(len(status["diagnostics"]), 2)
            diagnostics = json.loads((root / status["diagnosticsPath"]).read_text(encoding="utf-8"))
            self.assertEqual(diagnostics["format"], DIAGNOSTICS_FORMAT)
            self.assertEqual(len(status["reproducerPaths"]), 2)
            self.assertFalse((root / "deck.yaml").exists())

    def test_success_records_incremental_evidence_and_focused_tests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = write_repair_repository(root)
            workspace = root / "output/conversion-repair/current"

            def successful_build(**values):
                values["progress"]("converting", {"slideCount": 2})
                workspace.mkdir(parents=True, exist_ok=True)
                html = workspace / "presentation.repair.bento.html"
                report = workspace / "conversion-report.json"
                html.write_text("result", encoding="utf-8")
                report.write_text("{}", encoding="utf-8")
                return HtmlBuildResult(
                    html, workspace / "presentation.repair.bento.json", report,
                    (), (), {"slides": []}, {"incrementalCache": {"sourceHits": 2}},
                )

            with patch("scripts.run_conversion_repair.load_state", return_value=state):
                result = run_repair(
                    args_for(root, changed_paths=["bento_converter/html_converter.py"]),
                    build=successful_build,
                )

            self.assertEqual(result, 0)
            status = json.loads((root / "output/conversion-repair/status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "succeeded")
            self.assertEqual(status["incrementalCache"]["sourceHits"], 2)
            self.assertTrue(any(item["testId"] == "tests.test_html_first.HtmlConversionTests" for item in status["focusedTests"]))
            self.assertFalse(status["formalArtifactsChanged"])


@unittest.skipUnless(os.environ.get("BENTO_BROWSER_TEST") == "1", "Set BENTO_BROWSER_TEST=1 for repair browser integration.")
class ConversionRepairBrowserTests(unittest.TestCase):
    def test_second_repair_build_reuses_complete_slide_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = write_repair_repository(root, real_base=True)
            arguments = args_for(root)
            with patch("scripts.run_conversion_repair.load_state", return_value=state):
                self.assertEqual(run_repair(arguments), 0)
                first = json.loads((root / "output/conversion-repair/status.json").read_text(encoding="utf-8"))
                self.assertEqual(run_repair(arguments), 0)
                second = json.loads((root / "output/conversion-repair/status.json").read_text(encoding="utf-8"))
            self.assertEqual(first["incrementalCache"]["sourceHits"], 0)
            self.assertEqual(second["incrementalCache"]["sourceHits"], 2)
            self.assertEqual(second["incrementalCache"]["bentoHits"], 2)
            self.assertEqual(second["incrementalCache"]["comparisonHits"], 2)


if __name__ == "__main__":
    unittest.main()
