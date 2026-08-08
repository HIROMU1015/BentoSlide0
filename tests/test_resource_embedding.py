from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bento_converter.errors import ConversionError
from bento_converter.resource_embedding import (
    ResourceContext,
    embed_chart_option_resources,
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

    def test_external_svg_fragments_survive_href_xlink_and_css_embedding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "symbols.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
            context = self.context(root)
            markup = embed_markup_resources(
                '<svg><use href="symbols.svg#symbol-a"/><use xlink:href="symbols.svg#symbol-b"/>'
                '<rect style="filter:url(symbols.svg#blur);clip-path:url(symbols.svg#clip-a);fill:url(symbols.svg#gradient-a)"/></svg>',
                context=context,
            )
            for fragment in ("#symbol-a", "#symbol-b", "#blur", "#clip-a", "#gradient-a"):
                self.assertIn(fragment, markup)
            self.assertEqual(markup.count("data:image/svg+xml;base64,"), 5)
            self.assertNotIn("symbols.svg", markup)

    def test_registry_asset_fragment_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            context = self.context(Path(temporary))
            context.asset_lookup = lambda _: "data:image/svg+xml;base64,PHN2Zy8+"
            self.assertEqual(
                resolve_embedded_resource("asset:symbols#symbol-a", context=context),
                "data:image/svg+xml;base64,PHN2Zy8+#symbol-a",
            )

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

    def test_recursive_scan_covers_assets_media_chart_svg_theme_and_nested_values(self) -> None:
        document = {
            "assets": {"bad": "../asset.png", "good": "data:image/png;base64,AAAA"},
            "theme": {"backgroundImage": "url(theme.png)"},
            "slides": [{"id": "s", "elements": [
                {"id": "media", "type": "media", "src": "data:video/mp4;base64,AAAA", "poster": "poster.png"},
                {"id": "chart", "type": "chart", "option": {"series": [{"symbol": "image://relative.png"}]}},
                {"id": "svg", "type": "svg", "markup": '<svg><image href="local.svg#figure"/></svg>'},
            ]}],
            "meta": {"nested": [{"download": "file:///tmp/local.png", "thumbnail": "nested.png"}]},
        }
        scan = scan_document_resources(document)
        self.assertFalse(scan["passed"])
        fields = {item["field"] for item in scan["unresolved"]}
        self.assertTrue({"assets.bad", "poster", "option.series[0].symbol", "markup", "theme.backgroundImage", "meta.nested.[].download", "meta.nested.[].thumbnail"}.issubset(fields))
        self.assertGreaterEqual(scan["embeddedResources"], 2)
        self.assertTrue({"assets", "mediaPoster", "chartOption", "svgMarkup", "theme"}.issubset(scan["byCategory"]))

    def test_recursive_scan_allows_data_fragments_and_does_not_flag_prose(self) -> None:
        document = {
            "assets": {"symbols": "data:image/svg+xml;base64,PHN2Zy8+#symbol-a"},
            "slides": [{"id": "s", "elements": [
                {"id": "text", "type": "text", "html": "Example ../assets/figure.png and C:/paper/image.png"},
                {"id": "chart", "type": "chart", "option": {"symbol": "image://data:image/png;base64,AAAA"}},
            ]}],
        }
        scan = scan_document_resources(document)
        self.assertTrue(scan["passed"])
        self.assertEqual(scan["unresolved"], [])

    def test_recursive_scan_ignores_collaboration_session_metadata(self) -> None:
        document = {
            "collab": {"room": "team/deck", "sync": "client/session.json"},
            "slides": [{"id": "s", "elements": [
                {"id": "image", "type": "image", "src": "missing.png"},
            ]}],
        }
        scan = scan_document_resources(document)
        self.assertEqual(scan["unresolved"], [
            {"slideId": "s", "elementId": "image", "field": "src", "value": "$LOCAL_RESOURCE"},
        ])

    def test_chart_option_local_resources_are_embedded_recursively(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "point.png").write_bytes(b"point")
            context = self.context(root)
            option = embed_chart_option_resources(
                {"series": [{"symbol": "image://point.png", "decal": {"color": "url(point.png)"}}]},
                context=context,
            )
            self.assertTrue(option["series"][0]["symbol"].startswith("image://data:image/png;base64,"))
            self.assertIn("url(\"data:image/png;base64,", option["series"][0]["decal"]["color"])


if __name__ == "__main__":
    unittest.main()
