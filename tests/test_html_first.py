"""Tests for the reusable HTML/CSS + registry conversion path."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from bento_converter.bento_validator import validate_bento_doc
from bento_converter.html_converter import convert_html_layout
from bento_converter.html_layout import LayoutResult
from bento_converter.html_pipeline import build_from_html
from bento_converter.html_source import SourceChapter, discover_chapters, merge_registries

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
HTML_FIXTURES = FIXTURES / "html_first"


def element(element_id: str, element_type: str, **updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": element_id, "type": element_type, "exportMode": "auto", "domIndex": 0,
        "x": 10, "y": 10, "w": 200, "h": 100, "scrollWidth": 200, "scrollHeight": 100,
        "rotation": 0, "opacity": 1, "z": 0, "text": "Hello", "html": "Hello",
        "outerHTML": "<div>Hello</div>", "svg": None, "src": None,
        "style": {"color": "rgb(0, 0, 0)", "backgroundColor": "rgb(255, 255, 255)", "borderColor": "rgb(0, 0, 0)", "borderWidth": 1, "borderRadius": 0, "fontSize": 20, "fontFamily": "Arial", "fontWeight": 400, "lineHeight": 1.2, "letterSpacing": 0, "textAlign": "left", "verticalAlign": "baseline"},
    }
    value.update(updates)
    return value


class HtmlSourceTests(unittest.TestCase):
    def test_fixture_chapters_sort_and_merge_without_losing_registry_data(self) -> None:
        chapters = discover_chapters(HTML_FIXTURES, HTML_FIXTURES)
        self.assertEqual([chapter.chapter_id for chapter in chapters], ["chapter-01", "chapter-02"])
        registry = merge_registries(chapters)
        self.assertEqual(registry["equations"]["energy"]["latex"], "E = mc^2")
        self.assertIn("chapter-combine", registry["protected"]["slideIds"])

    def test_unknown_registry_version_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.preview.html").write_text("<section class='slide' data-slide-id='a'></section>", encoding="utf-8")
            (root / "a.registry.json").write_text(json.dumps({"format": "future", "chapterId": "a"}), encoding="utf-8")
            with self.assertRaisesRegex(Exception, "bento/html-registry/v1"):
                discover_chapters(root, root)


class HtmlConversionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.chapter = SourceChapter("c", Path("c.html"), Path("c.registry.json"), {})
        self.registry = {
            "document": {"title": "Fixture", "modified": "2026-08-02T00:00:00Z"},
            "assets": {}, "equations": {"eq": {"latex": "x^2"}}, "figures": {}, "tables": {}, "charts": {},
            "protected": {"slideIds": [], "elementIds": [], "requiredText": []},
        }

    def convert(self, elements: list[dict[str, object]]):
        layout = LayoutResult(({
            "id": "slide", "background": "rgb(255, 255, 255)", "transition": "none", "notes": "note",
            "layout": "free", "elements": elements,
        },), (), {}, "test Chromium")
        return convert_html_layout(layout, self.registry, [self.chapter])

    def test_native_text_equation_shape_table_chart_svg_and_media(self) -> None:
        values = [
            element("text", "text", html="<strong>Safe</strong><script>bad()</script>"),
            element("eq", "equation", equationId="eq"),
            element("shape", "shape", shape="rounded"),
            element("table", "table", table={"rows": [["A", "B"], ["1", "2"]], "headerRows": 1}),
            element("chart", "chart", chartOption={"_bentoPreset": "line", "series": [{"type": "line", "data": [1]}]}),
            element("svg", "svg", svg="<svg xmlns='http://www.w3.org/2000/svg'></svg>"),
            element("media", "media", src="data:audio/wav;base64,", mediaKind="audio", controls=True),
        ]
        result = self.convert(values)
        native = {item["id"]: item for item in result.document["slides"][0]["elements"]}
        self.assertEqual(native["eq"]["html"], "$$x^2$$")
        self.assertNotIn("script", native["text"]["html"])
        self.assertEqual(native["shape"]["shape"], "rect")
        self.assertEqual(native["table"]["columns"], [{"w": 1}, {"w": 1}])
        self.assertEqual(native["table"]["rows"][0]["cells"][0]["html"], "A")
        self.assertEqual(native["chart"]["preset"], "line")
        self.assertEqual(native["svg"]["type"], "svg")
        self.assertEqual(native["media"]["kind"], "audio")
        validate_bento_doc(result.document)

    def test_native_failure_localizes_svg_fallback_and_bounds_correction(self) -> None:
        source = element("bad", "shape", shape="hexagon", exportMode="native", x=-10, y=700, w=200, h=80)
        result = self.convert([source])
        output = result.document["slides"][0]["elements"][0]
        self.assertEqual(output["type"], "svg")
        self.assertEqual((output["x"], output["y"]), (0, 640))
        self.assertEqual(result.report["summary"]["strategies"]["svg"], 1)
        self.assertEqual(result.report["corrections"][0]["contentChanged"], False)

    def test_explicit_image_and_ignore_modes_are_reported(self) -> None:
        raster = element("raster", "complex", exportMode="image")
        ignored = element("guide", "text", exportMode="ignore", domIndex=1)
        layout = LayoutResult(({
            "id": "slide", "background": "rgb(255, 255, 255)", "transition": "none", "notes": "",
            "layout": "custom", "elements": [raster, ignored],
        },), (), {"slide/raster": "data:image/png;base64,AAAA"}, "test Chromium")
        result = convert_html_layout(layout, self.registry, [self.chapter])
        self.assertEqual([item["type"] for item in result.document["slides"][0]["elements"]], ["image"])
        self.assertEqual(result.report["summary"]["imageFallback"], 1)
        self.assertEqual(result.report["summary"]["strategies"]["ignore"], 1)


@unittest.skipUnless(os.environ.get("BENTO_BROWSER_TEST") == "1", "Set BENTO_BROWSER_TEST=1 for Chromium HTML-first integration.")
class HtmlFirstBrowserIntegrationTests(unittest.TestCase):
    def test_17_feature_matrix_builds_complete_evidence_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output" / "presentation.bento.html"
            result = build_from_html(
                html_dir=HTML_FIXTURES,
                registry_dir=HTML_FIXTURES,
                base_path=ROOT / "Bento_Slides.base.bento.html",
                output_path=output,
            )
            self.assertEqual(len(result.document["slides"]), 19)
            self.assertTrue(result.json_path.is_file())
            self.assertTrue(result.report_path.is_file())
            self.assertEqual(len(result.source_screenshots), 19)
            self.assertEqual(len(result.bento_screenshots), 19)
            self.assertTrue(result.report["runtimeIntegrity"])
            self.assertTrue(result.report["visualComparison"]["passed"])
            self.assertGreaterEqual(result.report["summary"]["strategies"]["svg"], 2)
            self.assertEqual(result.report["summary"]["nativeTable"], 1)
            self.assertEqual(result.report["summary"]["nativeChart"], 2)
            self.assertEqual(result.report["summary"]["nativeImage"], 1)
            self.assertEqual(result.report["summary"]["unresolvedWarnings"], 0)
            self.assertTrue(result.report["browserCheck"]["serialize_roundtrip"])
            self.assertEqual(result.report["browserCheck"]["rendered_slide_count"], 19)
            self.assertIsInstance(result.document["assets"]["fixture-image"], str)
            image = next(element for slide in result.document["slides"] for element in slide["elements"] if element["id"] == "image-native")
            self.assertEqual(image["src"], result.document["assets"]["fixture-image"])
            state = next(slide for slide in result.document["slides"] if slide["id"] == "state-detail")
            self.assertEqual(state["stateOf"], "state-base")
            self.assertTrue(all(element["x"] >= 0 and element["y"] >= 0 and element["x"] + element["w"] <= 1280 and element["y"] + element["h"] <= 720 for slide in result.document["slides"] for element in slide["elements"]))


if __name__ == "__main__":
    unittest.main()
