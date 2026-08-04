from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bento_converter.errors import ConversionError
from bento_converter.resource_embedding import (
    ResourceContext,
    embed_markup_resources,
    replace_css_urls,
    resolve_embedded_resource,
    scan_document_resources,
)


class ResourceEmbeddingTests(unittest.TestCase):
    def context(self, root: Path) -> ResourceContext:
        return ResourceContext(root / "chapter.preview.html", "slide", "complex", "asset", [])

    def test_foreign_object_relative_img_is_embedded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "image.png").write_bytes(b"png-relative")
            context = self.context(root)
            markup = embed_markup_resources('<foreignObject><img src="image.png"></foreignObject>', context=context)
            self.assertIn("data:image/png;base64,", markup)
            self.assertNotIn("image.png", markup)
            self.assertEqual(context.records[0]["source"], "$SOURCE_ROOT/image.png")

    def test_foreign_object_file_url_is_embedded_without_absolute_report_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "absolute.png"
            image.write_bytes(b"png-file")
            context = self.context(root)
            markup = embed_markup_resources(f'<img src="{image.resolve().as_uri()}">', context=context)
            self.assertIn("data:image/png;base64,", markup)
            self.assertNotIn("file:", markup)
            self.assertEqual(context.records[0]["source"], "$SOURCE_ROOT/absolute.png")

    def test_css_background_and_multiple_urls_are_embedded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "one.png").write_bytes(b"one")
            (root / "two.svg").write_text("<svg/>", encoding="utf-8")
            context = self.context(root)
            css = replace_css_urls("background:url(one.png),url('two.svg');mask-image:url(\"one.png\")", context=context)
            self.assertEqual(css.count("data:"), 3)
            self.assertNotIn("one.png", css)
            self.assertNotIn("two.svg", css)

    def test_inline_svg_image_embeds_but_fragment_stays_local(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "figure.png").write_bytes(b"figure")
            context = self.context(root)
            markup = embed_markup_resources(
                '<svg><defs><g id="symbol-a"></g></defs><image href="figure.png"/><use xlink:href="#symbol-a"/></svg>',
                context=context,
            )
            self.assertIn("data:image/png;base64,", markup)
            self.assertIn('xlink:href="#symbol-a"', markup)

    def test_data_http_and_fragment_references_are_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            context = self.context(Path(temporary))
            for value in ("data:image/png;base64,AAAA", "https://example.com/image.png", "#gradient"):
                self.assertEqual(resolve_embedded_resource(value, context=context), value)
            self.assertFalse(context.records)

    def test_registry_asset_reference_is_embedded_and_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            context = self.context(Path(temporary))
            context.asset_lookup = lambda asset_id: "data:image/png;base64,AAAA" if asset_id == "figure" else ""
            self.assertEqual(resolve_embedded_resource("asset:figure", context=context), "data:image/png;base64,AAAA")
            self.assertEqual(context.records[0]["source"], "asset:figure")

    def test_missing_local_asset_fails_with_redacted_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            context = self.context(Path(temporary))
            with self.assertRaisesRegex(ConversionError, r"slideId='slide'.*elementId='complex'.*\$SOURCE_ROOT/missing.png"):
                resolve_embedded_resource("missing.png", context=context)

    def test_resource_scan_distinguishes_prose_from_resource_fields(self) -> None:
        document = {
            "slides": [{"id": "s", "elements": [
                {"id": "text", "type": "text", "html": "Example path C:/paper/assets/figure.png"},
                {"id": "image", "type": "image", "src": "../assets/figure.png"},
            ]}],
        }
        scan = scan_document_resources(document)
        self.assertFalse(scan["passed"])
        self.assertEqual(scan["unresolved"], [{"slideId": "s", "elementId": "image", "field": "src", "value": "$LOCAL_RESOURCE"}])


if __name__ == "__main__":
    unittest.main()
