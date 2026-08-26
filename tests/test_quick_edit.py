from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from bento_converter.authoring_storage import AuthoringArtifactStorage
from bento_converter.errors import BentoConverterError
from bento_converter.html_document import extract_bento_doc, load_html
from bento_converter.quick_edit import (
    QUICK_PLAN_FORMAT,
    compile_quick_edit_plan,
    run_quick_edit,
    validate_quick_edit_plan,
)
from bento_converter.work_editor import create_work_editor_server
from scripts.apply_bento_quick_edits import main as quick_edit_main


ROOT = Path(__file__).resolve().parents[1]


class QuickEditPlanTests(unittest.TestCase):
    def document(self) -> dict:
        document = extract_bento_doc(load_html(ROOT / "demo.bento.html"))
        slide = document["slides"][0]
        shape = copy.deepcopy(slide["elements"][0])
        shape.update(id="deck-background", x=0, y=0, w=1280, h=720)
        footer = copy.deepcopy(slide["elements"][2])
        footer.update(id="deck-footer", x=72, y=680, w=1136, h=20)
        slide["elements"] = [shape, *slide["elements"][:5], footer]
        return document

    def test_slide_number_and_body_group_compile_without_background_header_or_footer(self) -> None:
        document = self.document()
        primitive, report = compile_quick_edit_plan(document, {
            "format": QUICK_PLAN_FORMAT, "slide": 1,
            "operations": [{"op": "shift", "target": "body", "dy": 10}],
        })
        changed_ids = {operation["elementId"] for operation in primitive["operations"]}
        self.assertNotIn("deck-background", changed_ids)
        self.assertNotIn("slide-1-title", changed_ids)
        self.assertNotIn("deck-footer", changed_ids)
        self.assertEqual(changed_ids, {"slide-1-shape", "slide-1-lead", "slide-1-body", "slide-1-shape-label"})
        self.assertEqual(report["slideNumber"], 1)
        self.assertEqual(report["boundsCheck"], "pass")

    def test_text_and_shape_styles_resolve_semantic_types(self) -> None:
        primitive, report = compile_quick_edit_plan(self.document(), {
            "format": QUICK_PLAN_FORMAT, "slide": "demo-slide-1",
            "operations": [
                {"op": "text-style", "target": "body", "properties": {"color": "#7c3aed"}},
                {"op": "shape-style", "target": "shapes", "properties": {"fill": "#eaf2fb"}},
            ],
        })
        text_ops = [operation for operation in primitive["operations"] if operation["op"] == "set-text-style"]
        shape_ops = [operation for operation in primitive["operations"] if operation["op"] == "set-shape"]
        self.assertEqual({operation["elementId"] for operation in text_ops},
                         {"slide-1-lead", "slide-1-body", "slide-1-shape-label"})
        self.assertEqual({operation["elementId"] for operation in shape_ops}, {"slide-1-shape"})
        self.assertEqual(report["primitiveOperationCount"], 4)

    def test_resize_align_distribute_and_fit_compile_from_filtered_ids(self) -> None:
        document = self.document()
        slide = document["slides"][0]
        template = copy.deepcopy(next(element for element in slide["elements"] if element["id"] == "slide-1-shape"))
        cards = []
        for index, x in enumerate((100, 360, 760), start=1):
            card = copy.deepcopy(template)
            card.update(id=f"card-{index}", x=x, y=300, w=120, h=80)
            cards.append(card)
        slide["elements"].extend(cards)
        primitive, report = compile_quick_edit_plan(document, {
            "format": QUICK_PLAN_FORMAT, "slide": 1,
            "operations": [
                {"op": "distribute", "target": {"group": "body-shapes", "idContains": "card-"}, "axis": "x"},
                {"op": "align", "target": {"group": "body-shapes", "idContains": "card-"}, "alignment": "top"},
                {"op": "resize-group", "target": {"group": "body-shapes", "idContains": "card-"},
                 "scaleX": .9, "scaleY": .9},
                {"op": "fit-within-slide", "target": {"group": "body-shapes", "idContains": "card-"},
                 "margin": 32},
            ],
        })
        self.assertEqual(len(report["resolvedOperations"]), 4)
        self.assertEqual(report["boundsCheck"], "pass")
        self.assertTrue(all(operation["elementId"].startswith("card-") for operation in primitive["operations"]))

    def test_bounds_preflight_blocks_outside_slide_before_save(self) -> None:
        with self.assertRaisesRegex(BentoConverterError, "outside the slide"):
            compile_quick_edit_plan(self.document(), {
                "format": QUICK_PLAN_FORMAT, "slide": 1,
                "operations": [{"op": "shift", "target": "body", "dy": 1000}],
            })

    def test_rejects_invalid_plan_and_non_loopback_dry_run(self) -> None:
        with self.assertRaises(BentoConverterError):
            validate_quick_edit_plan({"format": QUICK_PLAN_FORMAT, "slide": 0, "operations": []})
        with self.assertRaisesRegex(BentoConverterError, "loopback"):
            run_quick_edit("https://example.com/", {
                "format": QUICK_PLAN_FORMAT, "slide": 1,
                "operations": [{"op": "shift", "dy": 1}],
            }, dry_run=True)

    def test_cli_rejects_report_collision_before_running_an_edit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = root / "quick.json"
            plan.write_text(json.dumps({
                "format": QUICK_PLAN_FORMAT,
                "slide": 1,
                "operations": [{"op": "shift", "target": "body", "dy": 1}],
            }), encoding="utf-8")
            with patch(
                "scripts.apply_bento_quick_edits.validate_edit_report_path",
                side_effect=BentoConverterError("report collision"),
            ), patch("scripts.apply_bento_quick_edits.run_quick_edit") as run:
                result = quick_edit_main([
                    str(plan), "--editor-url", "http://127.0.0.1:8765/",
                    "--report", str(root / "unsafe.json"),
                ])
            self.assertEqual(result, 2)
            run.assert_not_called()


@unittest.skipUnless(
    os.environ.get("BENTO_BROWSER_TEST") == "1",
    "Set BENTO_BROWSER_TEST=1 to run quick edit integration tests.",
)
class QuickEditIntegrationTests(unittest.TestCase):
    def test_quick_edit_resolves_current_revision_and_saves_only_one_slide(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "presentation.generated.bento.html"
            target = root / "presentation.authoring.bento.html"
            source.write_bytes((ROOT / "demo.bento.html").read_bytes())
            source_registry = root / "merged-registry.json"
            source_registry.write_text(json.dumps({
                "format": "bento/html-registry/v2", "unitId": "deck", "sources": {},
                "document": {}, "assets": {}, "fonts": {},
                "equations": {"hamiltonian_split": {"latex": "H = H_0 + \\alpha H_1"}},
                "figures": {}, "tables": {}, "charts": {},
                "protected": {"slideIds": [], "elementIds": [], "requiredText": []},
            }), encoding="utf-8")
            storage = AuthoringArtifactStorage(
                source=source, source_registry=source_registry, target=target,
                target_registry=root / "presentation.authoring.registry.json", repository=root,
            )
            storage.acquire_writer_lease()
            server = create_work_editor_server(storage, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            editor = f"http://127.0.0.1:{server.server_address[1]}/"
            before = extract_bento_doc(load_html(target))
            try:
                report = run_quick_edit(editor, {
                    "format": QUICK_PLAN_FORMAT, "slide": 1,
                    "operations": [{
                        "op": "text-style", "target": {"group": "body-text", "elementIds": ["slide-1-lead"]},
                        "properties": {"color": "#7C3AED"},
                    }],
                })
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
            after = extract_bento_doc(load_html(target))
            lead = next(element for element in after["slides"][0]["elements"] if element["id"] == "slide-1-lead")
            self.assertEqual(lead["color"], "#7C3AED")
            self.assertEqual(after["slides"][1], before["slides"][1])
            self.assertEqual(report["changedSlideIds"], ["demo-slide-1"])
            self.assertEqual(report["quickEdit"]["slideNumber"], 1)


if __name__ == "__main__":
    unittest.main()
