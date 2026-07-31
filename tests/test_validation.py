from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from bento_converter.bento_validator import validate_bento_doc
from bento_converter.converter import convert_design
from bento_converter.design_validator import validate_design
from bento_converter.errors import BentoValidationError, DesignValidationError

FIXTURES = Path(__file__).parent / "fixtures"


class DesignValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.design = json.loads(
            (FIXTURES / "gpt_bento_design.demo.json").read_text(encoding="utf-8")
        )

    def assert_design_error(self, mutator, *needles):
        design = copy.deepcopy(self.design)
        mutator(design)
        with self.assertRaises(DesignValidationError) as context:
            validate_design(design)
        message = str(context.exception)
        for needle in needles:
            self.assertIn(needle, message)

    def test_duplicate_slide_id_error(self):
        self.assert_design_error(
            lambda d: d["slides"].__setitem__(1, {**d["slides"][1], "id": d["slides"][0]["id"]}),
            "slide.id",
            "Use a document-wide unique slide id",
        )

    def test_duplicate_element_id_error_has_context(self):
        def mutate(design):
            design["slides"][0]["elements"][1]["id"] = design["slides"][0]["elements"][0]["id"]

        self.assert_design_error(mutate, "slideId='demo-slide-1'", "element.id", "unique within this slide")

    def test_out_of_bounds_error(self):
        self.assert_design_error(
            lambda d: d["slides"][0]["elements"][3].__setitem__("x", 1200),
            "elementId='slide-1-shape'",
            "Keep the complete frame inside",
        )

    def test_unknown_type_error(self):
        self.assert_design_error(
            lambda d: d["slides"][0]["elements"][0].__setitem__("type", "image"),
            "element.type",
            "image",
        )

    def test_unknown_style_is_warning(self):
        design = copy.deepcopy(self.design)
        design["slides"][0]["elements"][1]["style"]["unsupportedGlow"] = 12
        report = validate_design(design)
        self.assertTrue(any("unsupportedGlow" in warning for warning in report.warnings))

    def test_bento_validation_rejects_duplicate_id(self):
        document = convert_design(self.design).document
        document["slides"][0]["elements"][1]["id"] = document["slides"][0]["elements"][0]["id"]
        with self.assertRaises(BentoValidationError):
            validate_bento_doc(document)


if __name__ == "__main__":
    unittest.main()
