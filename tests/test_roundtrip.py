from __future__ import annotations

import json
import unittest
from pathlib import Path

from bento_converter.bento_validator import validate_bento_html, validate_conversion
from bento_converter.converter import convert_design
from bento_converter.html_document import embed_bento_doc, extract_bento_doc

FIXTURES = Path(__file__).parent / "fixtures"


class IntegrationRoundtripTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.design = json.loads(
            (FIXTURES / "gpt_bento_design.demo.json").read_text(encoding="utf-8")
        )
        cls.expected = json.loads(
            (FIXTURES / "expected_bento_doc.demo.json").read_text(encoding="utf-8")
        )
        cls.base = (FIXTURES / "Bento_Slides.base.fixture.bento.html").read_text(encoding="utf-8")

    def test_demo_conversion_matches_independent_expected_fixture(self):
        actual = convert_design(self.design).document
        self.assertEqual(actual, self.expected)
        validate_conversion(self.design, actual)

    def test_embed_extract_and_runtime_roundtrip(self):
        output = embed_bento_doc(self.base, self.expected)
        self.assertEqual(extract_bento_doc(output), self.expected)
        document, _ = validate_bento_html(output, base_html=self.base)
        self.assertEqual(document, self.expected)

    def test_same_inputs_produce_byte_identical_html(self):
        first_doc = convert_design(self.design).document
        second_doc = convert_design(self.design).document
        self.assertEqual(embed_bento_doc(self.base, first_doc), embed_bento_doc(self.base, second_doc))


if __name__ == "__main__":
    unittest.main()

