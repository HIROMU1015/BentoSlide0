from __future__ import annotations

import json
import unittest
from pathlib import Path

from bento_converter.errors import HtmlDocumentError
from bento_converter.html_document import (
    assert_runtime_integrity,
    embed_bento_doc,
    extract_bento_doc,
    locate_bento_doc,
    serialize_bento_doc,
    without_bento_doc_content,
)

FIXTURES = Path(__file__).parent / "fixtures"


class HtmlDocumentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base = (FIXTURES / "Bento_Slides.base.fixture.bento.html").read_text(encoding="utf-8")

    def test_locates_exactly_one_block(self):
        span = locate_bento_doc(self.base)
        self.assertEqual(self.base[span.content_start : span.content_end], "")

    def test_rejects_missing_or_duplicate_blocks(self):
        with self.assertRaises(HtmlDocumentError):
            locate_bento_doc("<html></html>")
        with self.assertRaises(HtmlDocumentError):
            locate_bento_doc(self.base + self.base)

    def test_embedding_roundtrip_and_runtime_identity(self):
        document = {"format": "bento/slides", "text": "日本語 < tag"}
        output = embed_bento_doc(self.base, document)
        self.assertEqual(extract_bento_doc(output), document)
        self.assertEqual(without_bento_doc_content(output), without_bento_doc_content(self.base))
        assert_runtime_integrity(self.base, output)

    def test_literal_less_than_is_escaped_in_embedded_json(self):
        serialized = serialize_bento_doc({"value": "x < y"})
        self.assertIn("\\u003c", serialized)
        self.assertNotIn("<", serialized)
        output = embed_bento_doc(self.base, {"value": "x < y"})
        span = locate_bento_doc(output)
        self.assertNotIn("<", output[span.content_start : span.content_end])

    def test_json_format_is_fixed_and_unicode_is_readable(self):
        serialized = serialize_bento_doc({"日本語": [1, 2]})
        self.assertEqual(serialized, json.dumps({"日本語": [1, 2]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    unittest.main()

