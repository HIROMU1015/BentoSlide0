from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from bento_converter.converter import convert_design, stable_doc_id, text_content_to_html

FIXTURES = Path(__file__).parent / "fixtures"
DOC_ID = "00000000-0000-4000-8000-000000000001"
MODIFIED = "2026-01-02T03:04:05Z"


class ConverterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.design = json.loads(
            (FIXTURES / "gpt_bento_design.demo.json").read_text(encoding="utf-8")
        )

    def convert(self, design=None):
        return convert_design(
            design or self.design,
            doc_id=DOC_ID,
            modified=MODIFIED,
        ).document

    def test_plain_text_and_newline_conversion(self):
        self.assertEqual(text_content_to_html("a\nb < c & d"), "a<br>b &lt; c &amp; d")
        document = self.convert()
        body = next(e for e in document["slides"][0]["elements"] if e["id"] == "slide-1-body")
        self.assertIn("<br>", body["html"])
        self.assertNotIn("\n", body["html"])

    def test_shape_conversion(self):
        document = self.convert()
        shape = document["slides"][0]["elements"][0]
        self.assertEqual(shape["id"], "slide-1-shape")
        self.assertEqual(shape["type"], "shape")
        self.assertEqual(shape["shape"], "rect")
        self.assertEqual(shape["radius"], 22)

    def test_latex_conversion_and_metadata(self):
        document = self.convert()
        equation = next(
            e for e in document["slides"][1]["elements"] if e["id"] == "hamiltonian-equation"
        )
        self.assertEqual(equation["type"], "text")
        self.assertEqual(equation["html"], "$$H = H_0 + \\alpha H_1$$")
        self.assertEqual(equation["equationId"], "hamiltonian_split")
        self.assertEqual(equation["latexSource"], "H = H_0 + \\alpha H_1")

    def test_z_order_uses_stable_sort(self):
        design = copy.deepcopy(self.design)
        elements = design["slides"][0]["elements"]
        elements[0]["z"] = 20
        elements[1]["z"] = 10
        elements[2]["z"] = 10
        document = self.convert(design)
        ids = [element["id"] for element in document["slides"][0]["elements"]]
        self.assertLess(ids.index(elements[1]["id"]), ids.index(elements[2]["id"]))
        self.assertGreater(ids.index(elements[0]["id"]), ids.index(elements[2]["id"]))

    def test_stable_doc_id_and_fixed_modified_are_deterministic(self):
        design = copy.deepcopy(self.design)
        design["document"].pop("docId", None)
        first = convert_design(design, modified=MODIFIED).document
        second = convert_design(design, modified=MODIFIED).document
        self.assertEqual(first, second)
        self.assertEqual(first["docId"], stable_doc_id(design))


if __name__ == "__main__":
    unittest.main()

