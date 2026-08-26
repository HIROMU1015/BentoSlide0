from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from bento_converter.authoring_storage import AuthoringArtifactStorage
from bento_converter.browser_edit import (
    PLAN_FORMAT,
    BrowserEditPostSaveError,
    run_browser_edit,
    validate_browser_edit_plan,
    validate_edit_report_path,
)
from bento_converter.browser_harness import BrowserHarness
from bento_converter.errors import BentoConverterError, BrowserCheckError
from bento_converter.html_document import embed_bento_doc, extract_bento_doc, load_html
from bento_converter.work_editor import create_work_editor_server
from bento_converter.work_editor_storage import WorkEditorStorage
from scripts.apply_bento_browser_edits import main as browser_edit_main


ROOT = Path(__file__).resolve().parents[1]


class FailPostSaveNetworkHarness(BrowserHarness):
    def __init__(self) -> None:
        super().__init__()
        self.network_checks = 0

    def assert_no_blocked_network(self, profile: str | None = None) -> None:
        self.network_checks += 1
        if self.network_checks >= 2:
            raise BrowserCheckError("simulated post-save network evidence failure")
        super().assert_no_blocked_network(profile)


class BrowserEditPlanTests(unittest.TestCase):
    @staticmethod
    def editor_status(root: Path) -> dict:
        return {
            "repository": str(root),
            "target": "output/presentation.authoring.bento.html",
            "artifactPaths": [
                "output/presentation.authoring.bento.html",
                "output/presentation.authoring.bento.json",
                "output/presentation.authoring.registry.json",
            ],
        }

    def test_validates_common_shape_and_text_operations(self) -> None:
        plan = validate_browser_edit_plan({
            "format": PLAN_FORMAT,
            "operations": [
                {
                    "op": "set-shape", "slideId": "slide-1", "elementId": "shape-1",
                    "properties": {"fill": "#eaf2fb", "x": 64, "radius": 14},
                },
                {
                    "op": "set-text-style", "slideId": "slide-1", "elementId": "text-1",
                    "properties": {"color": "#0f172a", "fontSizePt": 24},
                },
                {
                    "op": "set-geometry", "slideId": "slide-1", "elementId": "image-1",
                    "properties": {"x": 100, "y": 120, "w": 300, "h": 200},
                },
            ],
        })
        self.assertEqual(plan["operations"][0]["properties"]["fill"], "#EAF2FB")
        self.assertEqual(plan["operations"][1]["properties"]["color"], "#0F172A")
        self.assertEqual(plan["operations"][2]["properties"]["x"], 100.0)

    def test_rejects_unknown_operations_properties_and_invalid_colors(self) -> None:
        cases = [
            {"op": "run-javascript", "slideId": "slide-1"},
            {
                "op": "set-shape", "slideId": "slide-1", "elementId": "shape-1",
                "properties": {"html": "unsafe"},
            },
            {
                "op": "set-shape", "slideId": "slide-1", "elementId": "shape-1",
                "properties": {"fill": "red"},
            },
        ]
        for operation in cases:
            with self.subTest(operation=operation):
                with self.assertRaises(BentoConverterError):
                    validate_browser_edit_plan({"format": PLAN_FORMAT, "operations": [operation]})

    def test_rejects_equation_text_replacement_at_plan_execution_boundary(self) -> None:
        plan = validate_browser_edit_plan({
            "format": PLAN_FORMAT,
            "operations": [{
                "op": "set-text", "slideId": "demo-slide-2",
                "elementId": "hamiltonian-equation", "text": "not an equation",
            }],
        })
        self.assertEqual(plan["operations"][0]["op"], "set-text")

    def test_cli_persists_committed_post_save_failure_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps({
                "format": PLAN_FORMAT,
                "operations": [{
                    "op": "set-text", "slideId": "slide-1",
                    "elementId": "title", "text": "updated",
                }],
            }), encoding="utf-8")
            report_path = root / "report.json"
            committed_report = {
                "format": "bento/browser-edit-report/v1",
                "saveStatus": "committed",
                "validation": "failed",
            }
            failure = BrowserEditPostSaveError("post-save check", committed_report)
            with patch(
                "scripts.apply_bento_browser_edits.validate_edit_report_path",
                return_value=report_path.resolve(),
            ), patch("scripts.apply_bento_browser_edits.run_browser_edit", side_effect=failure):
                result = browser_edit_main([
                    str(plan_path), "--editor-url", "http://127.0.0.1:8765/",
                    "--report", str(report_path),
                ])
            self.assertEqual(result, 3)
            self.assertEqual(json.loads(report_path.read_text(encoding="utf-8")), committed_report)

    def test_cli_rejects_report_collision_before_running_an_edit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan_path = root / "plan.json"
            plan_path.write_text("{}", encoding="utf-8")
            with patch(
                "scripts.apply_bento_browser_edits.validate_edit_report_path",
                side_effect=BentoConverterError("report collision"),
            ), patch("scripts.apply_bento_browser_edits.run_browser_edit") as run:
                result = browser_edit_main([
                    str(plan_path), "--editor-url", "http://127.0.0.1:8765/",
                    "--report", str(root / "unsafe.json"),
                ])
            self.assertEqual(result, 2)
            run.assert_not_called()

    def test_report_path_rejects_artifacts_inputs_revisions_and_foreign_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            output = root / "output"
            output.mkdir()
            plan = root / "plan.json"
            plan.write_text("{}", encoding="utf-8")
            foreign = root / "notes.json"
            foreign.write_text(json.dumps({"format": "notes/v1"}), encoding="utf-8")
            status = self.editor_status(root)

            with patch("bento_converter.browser_edit.WorkEditorClient.get", return_value=status):
                for rejected in (
                    output / "presentation.authoring.bento.html",
                    output / "presentation.authoring.bento.json",
                    output / "presentation.authoring.registry.json",
                    output / "authoring-save-report.json",
                    output / "revisions/immutable.json",
                    plan,
                    foreign,
                ):
                    with self.subTest(rejected=rejected), self.assertRaises(BentoConverterError):
                        validate_edit_report_path(
                            "http://127.0.0.1:8765/", rejected, input_paths=(plan,),
                        )

    def test_report_path_allows_new_or_same_format_report_inside_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            report = root / "output/diagnostics/browser-edit.json"
            report.parent.mkdir(parents=True)
            status = self.editor_status(root)
            with patch("bento_converter.browser_edit.WorkEditorClient.get", return_value=status):
                self.assertEqual(
                    validate_edit_report_path("http://localhost:8765/", report),
                    report.resolve(),
                )
                report.write_text(json.dumps({"format": "bento/browser-edit-report/v1"}), encoding="utf-8")
                self.assertEqual(
                    validate_edit_report_path("http://localhost:8765/", report),
                    report.resolve(),
                )


@unittest.skipUnless(
    os.environ.get("BENTO_BROWSER_TEST") == "1",
    "Set BENTO_BROWSER_TEST=1 to run browser edit integration tests.",
)
class BrowserEditIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "presentation.generated.bento.html"
        self.target = self.root / "presentation.authoring.bento.html"
        self.source.write_bytes((ROOT / "demo.bento.html").read_bytes())
        self.source_registry = self.root / "merged-registry.json"
        self.source_registry.write_text(json.dumps({
            "format": "bento/html-registry/v2", "unitId": "deck", "sources": {},
            "document": {}, "assets": {}, "fonts": {},
            "equations": {"hamiltonian_split": {"latex": "H = H_0 + \\alpha H_1"}},
            "figures": {}, "tables": {}, "charts": {},
            "protected": {"slideIds": [], "elementIds": [], "requiredText": []},
        }), encoding="utf-8")
        self.target_registry = self.root / "presentation.authoring.registry.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def authoring_storage(self) -> AuthoringArtifactStorage:
        storage = AuthoringArtifactStorage(
            source=self.source, source_registry=self.source_registry,
            target=self.target, target_registry=self.target_registry,
            repository=self.root,
        )
        storage.acquire_writer_lease()
        return storage

    def test_reusable_ui_plan_saves_once_and_captures_only_changed_slide(self) -> None:
        storage = self.authoring_storage()
        server = create_work_editor_server(storage, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        editor = f"http://127.0.0.1:{server.server_address[1]}/"
        screenshots = self.root / "screenshots"
        before = extract_bento_doc(load_html(self.target))
        original_count = len(before["slides"][0]["elements"])
        plan = {
            "format": PLAN_FORMAT,
            "expectedDocumentRevision": storage.status()["documentRevision"],
            "operations": [
                {
                    "op": "set-shape", "slideId": "demo-slide-1",
                    "elementId": "slide-1-shape",
                    "properties": {"fill": "#EAF2FB", "stroke": "#4E79A7", "x": 790},
                },
                {
                    "op": "add-rectangle", "slideId": "demo-slide-1",
                    "order": "background-card",
                    "properties": {
                        "fill": "#E8F4F1", "stroke": "#2A7F78", "lineWidth": 2,
                        "radius": 14, "x": 72, "y": 500, "w": 420, "h": 90,
                    },
                },
            ],
        }
        try:
            report = run_browser_edit(editor, plan, screenshots_dir=screenshots)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        after = extract_bento_doc(load_html(self.target))
        shape = next(
            element for element in after["slides"][0]["elements"]
            if element["id"] == "slide-1-shape"
        )
        self.assertEqual(report["validation"], "pass")
        self.assertEqual(report["executionPath"], "authoring-api")
        self.assertEqual(report["validationScope"], "changed-slides")
        self.assertEqual(report["fullDeckBrowserCheck"], "deferred")
        self.assertEqual(report["changedSlideIds"], ["demo-slide-1"])
        self.assertEqual(report["executionPath"], "authoring-api")
        self.assertEqual(len(report["screenshots"]), 1)
        self.assertEqual(shape["fill"].upper(), "#EAF2FB")
        self.assertEqual(shape["stroke"].upper(), "#4E79A7")
        self.assertEqual(shape["x"], 790)
        self.assertEqual(len(after["slides"][0]["elements"]), original_count + 1)
        self.assertEqual(after["slides"][1], before["slides"][1])

    def test_text_duplicate_delete_and_align_use_reusable_ui_paths(self) -> None:
        storage = self.authoring_storage()
        server = create_work_editor_server(storage, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        editor = f"http://127.0.0.1:{server.server_address[1]}/"
        before = extract_bento_doc(load_html(self.target))
        original_ids = {element["id"] for element in before["slides"][0]["elements"]}
        try:
            report = run_browser_edit(editor, {
                "format": PLAN_FORMAT,
                "operations": [
                    {
                        "op": "set-text", "slideId": "demo-slide-1",
                        "elementId": "slide-1-title", "text": "ブラウザ操作で更新",
                    },
                    {
                        "op": "set-text-style", "slideId": "demo-slide-1",
                        "elementId": "slide-1-lead",
                        "properties": {"color": "#2A7F78", "fontSizePt": 20},
                    },
                    {
                        "op": "duplicate", "slideId": "demo-slide-1",
                        "elementId": "slide-1-note",
                    },
                    {
                        "op": "delete", "slideId": "demo-slide-1",
                        "elementId": "slide-1-shape-label",
                    },
                    {
                        "op": "align", "slideId": "demo-slide-1",
                        "elementIds": ["slide-1-title", "slide-1-shape"],
                        "alignment": "left",
                    },
                ],
            })
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        after = extract_bento_doc(load_html(self.target))
        elements = {element["id"]: element for element in after["slides"][0]["elements"]}
        added_ids = set(elements) - original_ids
        self.assertEqual(report["changedSlideIds"], ["demo-slide-1"])
        self.assertEqual(report["executionPath"], "authoring-api")
        self.assertEqual(elements["slide-1-title"]["html"], "ブラウザ操作で更新")
        self.assertEqual(elements["slide-1-lead"]["color"].upper(), "#2A7F78")
        self.assertAlmostEqual(elements["slide-1-lead"]["fontSize"], 26.67, places=2)
        self.assertEqual(elements["slide-1-title"]["x"], elements["slide-1-shape"]["x"])
        self.assertNotIn("slide-1-shape-label", elements)
        self.assertEqual(len(added_ids), 1)

    def test_color_only_edit_commits_for_rich_text(self) -> None:
        source_html = load_html(self.source)
        source_document = extract_bento_doc(source_html)
        source_lead = next(
            element for element in source_document["slides"][0]["elements"]
            if element["id"] == "slide-1-lead"
        )
        source_lead["html"] = "<strong>強調</strong>を含む本文"
        self.source.write_text(embed_bento_doc(source_html, source_document), encoding="utf-8")
        storage = self.authoring_storage()
        server = create_work_editor_server(storage, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        editor = f"http://127.0.0.1:{server.server_address[1]}/"
        try:
            report = run_browser_edit(editor, {
                "format": PLAN_FORMAT,
                "operations": [{
                    "op": "set-text-style", "slideId": "demo-slide-1",
                    "elementId": "slide-1-lead", "properties": {"color": "#7C3AED"},
                }],
            })
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        saved = extract_bento_doc(load_html(self.target))
        saved_lead = next(
            element for element in saved["slides"][0]["elements"]
            if element["id"] == "slide-1-lead"
        )
        self.assertEqual(report["changedSlideIds"], ["demo-slide-1"])
        self.assertEqual(saved_lead["html"], "<strong>強調</strong>を含む本文")
        self.assertEqual(saved_lead["color"].upper(), "#7C3AED")

    def test_no_op_plan_skips_save_and_keeps_revision(self) -> None:
        storage = self.authoring_storage()
        server = create_work_editor_server(storage, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        editor = f"http://127.0.0.1:{server.server_address[1]}/"
        before_document = extract_bento_doc(load_html(self.target))
        title = next(
            element for element in before_document["slides"][0]["elements"]
            if element["id"] == "slide-1-title"
        )
        before_status = storage.status()
        try:
            report = run_browser_edit(editor, {
                "format": PLAN_FORMAT,
                "operations": [{
                    "op": "set-text", "slideId": "demo-slide-1",
                    "elementId": "slide-1-title", "text": title["html"],
                }],
            })
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        after_status = storage.status()
        self.assertEqual(report["saveStatus"], "no-op")
        self.assertEqual(report["executionPath"], "authoring-api")
        self.assertEqual(report["changedSlideIds"], [])
        self.assertEqual(report["beforeDocumentRevision"], report["afterDocumentRevision"])
        self.assertEqual(after_status["documentRevision"], before_status["documentRevision"])
        self.assertEqual(after_status["backupCount"], before_status["backupCount"])

    def test_post_save_check_failure_reports_committed_edit_separately(self) -> None:
        storage = self.authoring_storage()
        server = create_work_editor_server(storage, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        editor = f"http://127.0.0.1:{server.server_address[1]}/"
        try:
            with FailPostSaveNetworkHarness() as harness:
                with self.assertRaises(BrowserEditPostSaveError) as caught:
                    run_browser_edit(editor, {
                        "format": PLAN_FORMAT,
                        "operations": [{
                            "op": "set-text", "slideId": "demo-slide-1",
                            "elementId": "slide-1-title", "text": "保存済み・検査は要再確認",
                        }],
                    }, harness=harness)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        after = extract_bento_doc(load_html(self.target))
        title = next(
            element for element in after["slides"][0]["elements"]
            if element["id"] == "slide-1-title"
        )
        report = caught.exception.report
        self.assertEqual(title["html"], "保存済み・検査は要再確認")
        self.assertEqual(report["saveStatus"], "committed")
        self.assertEqual(report["validation"], "failed")
        self.assertEqual(report["validationScope"], "post-save")
        self.assertEqual(report["changedSlideIds"], ["demo-slide-1"])
        self.assertIn("simulated post-save", report["postSaveError"])

    def test_final_mode_rejects_content_operations_before_save(self) -> None:
        storage = WorkEditorStorage(source=self.source, target=self.target)
        server = create_work_editor_server(storage, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        editor = f"http://127.0.0.1:{server.server_address[1]}/"
        before = self.target.read_bytes()
        try:
            with self.assertRaisesRegex(BentoConverterError, "not allowed"):
                run_browser_edit(editor, {
                    "format": PLAN_FORMAT,
                    "operations": [{
                        "op": "delete", "slideId": "demo-slide-1", "elementId": "slide-1-shape",
                    }],
                })
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertEqual(self.target.read_bytes(), before)

    def test_final_mode_allows_geometry_style_and_order(self) -> None:
        storage = WorkEditorStorage(source=self.source, target=self.target)
        server = create_work_editor_server(storage, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        editor = f"http://127.0.0.1:{server.server_address[1]}/"
        try:
            report = run_browser_edit(editor, {
                "format": PLAN_FORMAT,
                "operations": [
                    {
                        "op": "set-shape", "slideId": "demo-slide-1",
                        "elementId": "slide-1-shape",
                        "properties": {"fill": "#EAF2FB", "x": 790},
                    },
                    {
                        "op": "set-order", "slideId": "demo-slide-1",
                        "elementId": "slide-1-shape", "order": "backward",
                    },
                ],
            })
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        after = extract_bento_doc(load_html(self.target))
        shape = next(
            element for element in after["slides"][0]["elements"]
            if element["id"] == "slide-1-shape"
        )
        self.assertEqual(report["editingMode"], "final")
        self.assertEqual(report["changedSlideIds"], ["demo-slide-1"])
        self.assertEqual(shape["fill"].upper(), "#EAF2FB")
        self.assertEqual(shape["x"], 790)


if __name__ == "__main__":
    unittest.main()
