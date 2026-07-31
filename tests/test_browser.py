from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

from bento_converter.browser_check import run_browser_check
from bento_converter.converter import convert_design
from bento_converter.html_document import write_embedded_document

ROOT = Path(__file__).parent.parent
FIXTURES = ROOT / "tests" / "fixtures"


@unittest.skipUnless(
    os.environ.get("BENTO_BROWSER_TEST") == "1",
    "Set BENTO_BROWSER_TEST=1 to run the local Chrome/Playwright integration test.",
)
class BrowserIntegrationTests(unittest.TestCase):
    def test_generic_check_discovers_arbitrary_ids_types_and_slide_count(self):
        design = json.loads(
            (FIXTURES / "gpt_bento_design.demo.json").read_text(encoding="utf-8")
        )
        design["document"]["title"] = "Arbitrary research deck"
        third_slide = copy.deepcopy(design["slides"][1])
        design["slides"].append(third_slide)
        for slide_index, slide in enumerate(design["slides"]):
            slide["id"] = f"paper-section-{slide_index + 1}"
            for element_index, element in enumerate(slide["elements"]):
                element["id"] = f"paper-element-{slide_index + 1}-{element_index + 1}"

        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            design_path = temp / "paper-design.json"
            output_path = temp / "paper.bento.html"
            design_path.write_text(
                json.dumps(design, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            document = convert_design(design).document
            write_embedded_document(
                ROOT / "Bento_Slides.base.bento.html", output_path, document
            )
            report = run_browser_check(output_path, design_path=design_path)

        self.assertEqual(report.slide_count, 3)
        self.assertEqual(report.rendered_slide_count, 3)
        self.assertEqual(report.element_count, 16)
        self.assertEqual(len(report.checked_coordinates), 16)
        self.assertEqual(report.detected_types, {"latex": 2, "shape": 1, "text": 13})
        self.assertNotIn("slide-1-title", " ".join(report.checked_coordinates))
        self.assertTrue(report.ui_selection)
        self.assertTrue(report.api_text_edit)
        self.assertTrue(report.api_shape_move)
        self.assertTrue(report.api_equation_edit)
        self.assertTrue(report.api_equation_rerender)
        self.assertTrue(report.serialize_roundtrip)
        self.assertTrue(report.equation_id_preserved)
        self.assertTrue(report.latex_source_preserved)
        self.assertFalse(report.latex_source_auto_synced)


if __name__ == "__main__":
    unittest.main()
