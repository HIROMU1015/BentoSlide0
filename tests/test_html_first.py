"""Tests for the reusable HTML/CSS + registry conversion path."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from bento_converter.bento_validator import validate_bento_doc
from bento_converter.errors import BentoConverterError, BrowserCheckError
from bento_converter.html_converter import convert_html_layout
from bento_converter.html_change_review import collect_html_change_browser_evidence
from bento_converter.html_layout import LayoutResult
from bento_converter.html_pipeline import build_from_html
from bento_converter.html_source import SourceChapter, discover_chapters, discover_source_unit, merge_registries
from bento_converter.registry_document import content_digest

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
        self.assertEqual([chapter.chapter_id for chapter in chapters], ["chapter-01", "chapter-02", "chapter-03", "chapter-04"])
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

    def test_single_v2_source_unit_supports_japanese_and_space_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "日本語 deck"
            root.mkdir()
            html_path = root / "資料 preview.html"
            registry_path = root / "資料 registry.json"
            html_path.write_text("<section class='slide' data-slide-id='one'></section>", encoding="utf-8")
            registry_path.write_text(json.dumps({
                "format": "bento/html-registry/v2", "unitId": "deck", "sources": {},
                "assets": {}, "fonts": {}, "equations": {}, "figures": {}, "tables": {}, "charts": {},
                "protected": {"slideIds": [], "elementIds": [], "requiredText": []},
            }), encoding="utf-8")
            unit = discover_source_unit(html_path, registry_path)
            self.assertEqual(unit.unit_id, "deck")
            self.assertEqual(merge_registries([unit])["format"], "bento/html-registry/v2")

    def test_converter_requires_exactly_one_complete_source_form(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "out/presentation.bento.html"
            with self.assertRaisesRegex(Exception, "exactly one source form"):
                build_from_html(
                    base_path=ROOT / "Bento_Slides.base.bento.html", output_path=output,
                    html_path="a.html", registry_path="a.json", html_dir="chapters", registry_dir="chapters",
                )
            self.assertFalse(output.parent.exists())

    def test_source_unit_rejects_visual_file_digest_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "asset.png").write_bytes(b"changed bytes")
            html_path = root / "deck.preview.html"
            registry_path = root / "deck.registry.json"
            html_path.write_text("<section class='slide' data-slide-id='one'></section>", encoding="utf-8")
            registry_path.write_text(json.dumps({
                "format": "bento/html-registry/v2", "unitId": "deck", "sources": {},
                "assets": {"image": {
                    "path": "asset.png", "contentDigest": content_digest(b"original bytes"),
                    "origin": {"kind": "generated"},
                }},
                "fonts": {}, "equations": {}, "figures": {}, "tables": {}, "charts": {},
                "protected": {"slideIds": [], "elementIds": [], "requiredText": []},
            }), encoding="utf-8")
            with self.assertRaisesRegex(BentoConverterError, "contentDigest mismatch"):
                discover_source_unit(html_path, registry_path)


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
            element("svg", "svg", svg="<svg xmlns='http://www.w3.org/2000/svg' style='position:absolute;left:100px' viewBox='0 0 200 100'></svg>"),
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
        self.assertNotIn("position:absolute", native["svg"]["markup"])
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

    def test_explicit_native_still_falls_back_for_impossible_css(self) -> None:
        unsafe = element(
            "clipped", "text", exportMode="native",
            style={**element("unused", "text")["style"], "clipPath": "polygon(0 0,100% 0,50% 100%)"},
        )
        result = self.convert([unsafe])
        output = result.document["slides"][0]["elements"][0]
        decision = result.report["elements"][0]
        self.assertEqual(output["type"], "svg")
        self.assertEqual(decision["nativeCompatibility"], "localized-svg-recommended")
        self.assertIn("clip-path", decision["reason"])

    def test_complex_table_uses_local_svg_and_preserves_image_markup(self) -> None:
        cell_style = {"backgroundColor": "rgb(255,255,255)", "color": "rgb(0,0,0)", "fontSize": 14, "fontFamily": "Arial", "textAlign": "center"}
        complex_table = element(
            "complex-table", "table",
            table={
                "simpleTable": False,
                "complexityReasons": ["HTML table cell contains image/chart/complex content"],
                "rows": [[{
                    "html": '<img alt="dot" src="data:image/png;base64,AAAA">',
                    "rowSpan": 1, "colSpan": 1, "rowIndex": 0, "columnIndex": 0,
                    "rect": {"x": 0, "y": 0, "w": 200, "h": 100}, "style": cell_style,
                }]],
            },
        )
        result = self.convert([complex_table])
        output = result.document["slides"][0]["elements"][0]
        self.assertEqual(output["type"], "svg")
        self.assertIn("<img", output["markup"])
        self.assertEqual(result.report["summary"]["nativeTable"], 0)


@unittest.skipUnless(os.environ.get("BENTO_BROWSER_TEST") == "1", "Set BENTO_BROWSER_TEST=1 for Chromium HTML-first integration.")
class HtmlFirstBrowserIntegrationTests(unittest.TestCase):
    def test_post_apply_review_captures_only_affected_slides(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "deck.preview.html"
            registry_path = root / "deck.registry.json"
            source.write_text(
                "<!doctype html><style>html,body{margin:0}.slide{position:relative;width:1280px;height:720px;"
                "overflow:hidden;background:#fff}.text{position:absolute;width:400px;height:80px}</style>"
                "<main data-bento-deck>"
                "<section class='slide' data-slide-id='one' data-section-id='intro'>"
                "<div class='text' data-bento-id='one-title' data-bento-type='text' "
                "style='left:80px;top:80px'>One</div></section>"
                "<section class='slide' data-slide-id='two' data-section-id='method'>"
                "<div class='text' data-bento-id='two-title' data-bento-type='text' "
                "style='left:80px;top:80px'>Two</div></section></main>",
                encoding="utf-8",
            )
            registry_path.write_text(json.dumps({
                "format": "bento/html-registry/v2", "unitId": "deck", "sources": {},
                "document": {"title": "Post-apply review"},
                "assets": {}, "fonts": {}, "equations": {}, "figures": {},
                "tables": {}, "charts": {},
                "protected": {"slideIds": [], "elementIds": [], "requiredText": []},
            }), encoding="utf-8")

            evidence = collect_html_change_browser_evidence(
                html_path=source,
                registry_path=registry_path,
                affected_slide_ids=["two"],
                screenshots_dir=root / "screenshots",
            )

            self.assertEqual(evidence.report["status"], "pass")
            self.assertEqual(evidence.report["affectedSlideIds"], ["two"])
            self.assertEqual(set(evidence.screenshots), {"two"})
            self.assertTrue(evidence.screenshots["two"].is_file())
            self.assertTrue(evidence.environment["environmentDigest"].startswith("sha256:"))

    def test_visual_origins_embed_images_and_native_diagram_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "assets/source").mkdir(parents=True)
            (root / "assets/generated").mkdir(parents=True)
            Image.new("RGB", (40, 40), "#2563eb").save(root / "assets/source/original.png")
            Image.new("RGB", (40, 40), "#10b981").save(root / "assets/generated/concept.png")
            source = root / "deck.preview.html"
            source.write_text(
                "<!doctype html><style>body{margin:0}.slide{position:relative;width:1280px;height:720px;background:#fff}"
                ".item{position:absolute}.node{width:180px;height:90px;background:#dbeafe;border:2px solid #2563eb}</style>"
                "<section class='slide' data-slide-id='visuals' data-section-id='method'>"
                "<img class='item' data-bento-id='original' data-bento-type='image' data-asset-id='original-asset' "
                "data-figure-id='original-figure' src='assets/source/original.png' style='left:60px;top:80px;width:200px;height:160px'>"
                "<img class='item' data-bento-id='concept' data-bento-type='image' data-asset-id='concept-asset' "
                "data-figure-id='concept-figure' src='assets/generated/concept.png' style='left:300px;top:80px;width:200px;height:160px'>"
                "<div class='item node' data-bento-id='node-a' data-bento-type='shape' data-bento-shape='rect' data-figure-id='native-diagram' "
                "style='left:120px;top:360px'></div>"
                "<div class='item' data-bento-id='connector' data-bento-type='shape' data-bento-shape='line' data-figure-id='native-diagram' "
                "data-line-end='arrow' style='left:300px;top:400px;width:260px;height:8px;border-top:4px solid #334155'></div>"
                "<div class='item node' data-bento-id='node-b' data-bento-type='shape' data-bento-shape='rect' data-figure-id='native-diagram' "
                "style='left:560px;top:360px'></div>"
                "<div class='item' data-bento-id='node-label' data-bento-type='text' data-figure-id='native-diagram' "
                "style='left:140px;top:390px;width:140px;height:40px'>Native flow</div>"
                "</section>", encoding="utf-8",
            )
            origin = {"kind": "source-original", "sourceId": "paper", "locator": "Fig. 4, p. 8"}
            generated = {"kind": "generated"}
            registry_path = root / "deck.registry.json"
            registry_path.write_text(json.dumps({
                "format": "bento/html-registry/v2", "unitId": "deck",
                "sources": {"paper": {"path": "sources/private/paper.pdf", "type": "pdf"}},
                "document": {"title": "Visual origins"},
                "assets": {
                    "original-asset": {"path": "assets/source/original.png", "origin": origin,
                                       "contentDigest": content_digest((root / "assets/source/original.png").read_bytes()),
                                       "provenance": {"sourceId": "paper", "locator": "Fig. 4, p. 8"}},
                    "concept-asset": {"path": "assets/generated/concept.png", "role": "conceptual-illustration", "origin": generated,
                                      "contentDigest": content_digest((root / "assets/generated/concept.png").read_bytes())},
                },
                "figures": {
                    "original-figure": {"assetId": "original-asset", "origin": origin,
                                         "provenance": {"sourceId": "paper", "locator": "Fig. 4, p. 8"}},
                    "concept-figure": {"assetId": "concept-asset", "origin": generated},
                    "native-diagram": {"role": "derived-diagram", "origin": {"kind": "source-derived", "sources": [
                        {"sourceId": "paper", "locator": "Method flow, Sec. 3"},
                    ]}},
                },
                "fonts": {}, "equations": {}, "tables": {}, "charts": {},
                "protected": {"slideIds": [], "elementIds": [], "requiredText": []},
            }), encoding="utf-8")
            result = build_from_html(
                html_path=source, registry_path=registry_path,
                base_path=ROOT / "Bento_Slides.base.bento.html",
                output_path=root / "output/presentation.generated.bento.html", browser_check=False,
            )
            by_id = {element["id"]: element for element in result.document["slides"][0]["elements"]}
            for element_id, asset_id, figure_id in (
                ("original", "original-asset", "original-figure"),
                ("concept", "concept-asset", "concept-figure"),
            ):
                self.assertEqual(by_id[element_id]["type"], "image")
                self.assertTrue(by_id[element_id]["src"].startswith("data:image/png;base64,"))
                self.assertEqual(by_id[element_id]["assetId"], asset_id)
                self.assertEqual(by_id[element_id]["figureId"], figure_id)
            self.assertEqual(by_id["node-a"]["type"], "shape")
            self.assertEqual(by_id["connector"]["type"], "shape")
            self.assertEqual(by_id["node-label"]["type"], "text")
            for element_id in ("node-a", "connector", "node-b", "node-label"):
                self.assertEqual(by_id[element_id]["figureId"], "native-diagram")
            self.assertTrue(result.report["resourceScan"]["passed"])
            self.assertEqual(result.report["summary"]["unresolvedLocalResourceReferences"], 0)
            merged = json.loads((root / "output/diagnostics/merged-registry.json").read_text(encoding="utf-8"))
            self.assertEqual(merged["assets"]["original-asset"]["origin"], origin)
            self.assertEqual(merged["assets"]["concept-asset"]["origin"], generated)
            self.assertEqual(merged["figures"]["native-diagram"]["origin"]["kind"], "source-derived")

    def test_incremental_build_reuses_only_unchanged_slides(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "deck.preview.html"
            registry_path = root / "deck.registry.json"
            output = root / "output/presentation.generated.bento.html"

            def write_source(second_color: str) -> None:
                source.write_text(
                    "<!doctype html><style>body{margin:0}.slide{position:relative;width:1280px;height:720px;background:#fff}"
                    ".block{position:absolute;left:180px;top:160px;width:920px;height:400px}</style>"
                    "<section class='slide' data-slide-id='one'><div class='block' data-bento-id='one-block' "
                    "data-bento-type='shape' data-bento-shape='rect' style='background:#2563eb'></div></section>"
                    "<section class='slide' data-slide-id='two'><div class='block' data-bento-id='two-block' "
                    f"data-bento-type='shape' data-bento-shape='rect' style='background:{second_color}'></div></section>",
                    encoding="utf-8",
                )

            write_source("#7c3aed")
            registry_path.write_text(json.dumps({
                "format": "bento/html-registry/v2", "unitId": "deck", "sources": {},
                "document": {"title": "Incremental"}, "assets": {}, "fonts": {}, "equations": {},
                "figures": {}, "tables": {}, "charts": {},
                "protected": {"slideIds": [], "elementIds": [], "requiredText": []},
            }), encoding="utf-8")

            full = build_from_html(
                html_path=source, registry_path=registry_path,
                base_path=ROOT / "Bento_Slides.base.bento.html", output_path=output,
            )
            full_bytes = full.html_path.read_bytes()
            self.assertEqual(full.report["incrementalCache"]["sourceHits"], 0)
            self.assertEqual(full.report["incrementalCache"]["bentoHits"], 0)

            unchanged = build_from_html(
                html_path=source, registry_path=registry_path,
                base_path=ROOT / "Bento_Slides.base.bento.html", output_path=output,
                incremental=True,
            )
            self.assertEqual(unchanged.html_path.read_bytes(), full_bytes)
            self.assertEqual(unchanged.report["incrementalCache"]["sourceHits"], 2)
            self.assertEqual(unchanged.report["incrementalCache"]["bentoHits"], 2)
            self.assertEqual(unchanged.report["incrementalCache"]["comparisonHits"], 2)

            write_source("#059669")
            changed = build_from_html(
                html_path=source, registry_path=registry_path,
                base_path=ROOT / "Bento_Slides.base.bento.html", output_path=output,
                incremental=True,
            )
            self.assertEqual(changed.report["incrementalCache"]["sourceHits"], 1)
            self.assertEqual(changed.report["incrementalCache"]["sourceMisses"], 1)
            self.assertEqual(changed.report["incrementalCache"]["bentoHits"], 1)
            self.assertEqual(changed.report["incrementalCache"]["bentoMisses"], 1)

            environment = json.loads(
                (output.parent / "diagnostics/browser-environment.json").read_text(encoding="utf-8")
            )
            self.assertEqual(environment["format"], "bento/browser-environment/v1")
            self.assertEqual(
                set(environment["browserEnvironment"]["profiles"]),
                {"sourceLayout", "bentoCheck"},
            )
            self.assertNotIn("browserEnvironment", changed.document)

    def test_remote_source_resource_is_blocked_before_conversion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "remote.preview.html"
            source.write_text(
                "<!doctype html><style>.slide{position:relative;width:1280px;height:720px}</style>"
                "<section class='slide' data-slide-id='remote'>"
                "<img data-bento-id='remote-image' src='https://example.invalid/remote.png'>"
                "</section>",
                encoding="utf-8",
            )
            registry = root / "remote.registry.json"
            registry.write_text(json.dumps({
                "format": "bento/html-registry/v1", "chapterId": "remote",
            }), encoding="utf-8")
            with self.assertRaisesRegex(BrowserCheckError, "blocked remote"):
                build_from_html(
                    html_dir=root, registry_dir=root,
                    base_path=ROOT / "Bento_Slides.base.bento.html",
                    output_path=root / "output/presentation.bento.html",
                    browser_check=False,
                )
            environment = json.loads(
                (root / "output/diagnostics/browser-environment.json").read_text(encoding="utf-8")
            )
            blocked = environment["browserEnvironment"]["networkPolicy"]["blockedRequests"]
            self.assertTrue(any(item["host"] == "example.invalid" for item in blocked))

    def test_single_html_and_v2_registry_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "日本語 deck"
            root.mkdir()
            source = root / "deck preview.html"
            source.write_text(
                "<!doctype html><style>.slide{position:relative;width:1280px;height:720px}</style>"
                "<section class='slide' data-slide-id='single' data-section-id='intro'>"
                "<h1 data-bento-id='title' style='position:absolute;left:80px;top:80px'>Single deck</h1>"
                "</section>", encoding="utf-8",
            )
            registry_path = root / "deck registry.json"
            registry_path.write_text(json.dumps({
                "format": "bento/html-registry/v2", "unitId": "deck", "sources": {},
                "document": {"title": "Single"}, "assets": {}, "fonts": {}, "equations": {},
                "figures": {}, "tables": {}, "charts": {},
                "protected": {"slideIds": ["single"], "elementIds": ["title"], "requiredText": ["Single deck"]},
            }), encoding="utf-8")
            result = build_from_html(
                html_path=source, registry_path=registry_path,
                base_path=ROOT / "Bento_Slides.base.bento.html",
                output_path=root / "output/presentation.generated.bento.html", browser_check=False,
            )
            self.assertEqual([slide["id"] for slide in result.document["slides"]], ["single"])
            merged = json.loads((root / "output/diagnostics/merged-registry.json").read_text(encoding="utf-8"))
            self.assertEqual(merged["format"], "bento/html-registry/v2")

    def test_local_resources_inside_svg_fallback_are_self_contained_and_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            Image.new("RGB", (8, 8), "#2563eb").save(root / "asset.png")
            (root / "resource.preview.html").write_text(
                '<!doctype html><style>body{margin:0}.slide{position:relative;width:1280px;height:720px}'
                '.complex{position:absolute;left:100px;top:100px;width:500px;height:300px;'
                'background-image:url("asset.png"),url(\'asset.png\')}</style>'
                '<section class="slide" data-slide-id="resources">'
                '<div class="complex" data-bento-id="portable" data-bento-type="complex" data-bento-export="svg">'
                '<img src="asset.png"><svg viewBox="0 0 20 20"><image href="asset.png" width="20" height="20"/></svg>'
                '</div></section>',
                encoding="utf-8",
            )
            (root / "resource.registry.json").write_text(
                json.dumps({"format": "bento/html-registry/v1", "chapterId": "resources"}), encoding="utf-8"
            )
            first = build_from_html(
                html_dir=root, registry_dir=root, base_path=ROOT / "Bento_Slides.base.bento.html",
                output_path=root / "first" / "presentation.bento.html", browser_check=False,
            )
            second = build_from_html(
                html_dir=root, registry_dir=root, base_path=ROOT / "Bento_Slides.base.bento.html",
                output_path=root / "second" / "presentation.bento.html", browser_check=False,
            )
            markup = first.document["slides"][0]["elements"][0]["markup"]
            self.assertGreaterEqual(markup.count("data:image/png;base64,"), 4)
            self.assertNotIn("asset.png", markup)
            self.assertNotIn("file:", markup)
            self.assertTrue(first.report["resourceScan"]["passed"])
            self.assertGreaterEqual(first.report["summary"]["embeddedLocalAssets"], 4)
            self.assertEqual(first.report["summary"]["unresolvedLocalResourceReferences"], 0)
            self.assertTrue((first.html_path.parent / "diagnostics" / "resource-scan.json").is_file())
            serialized_report = json.dumps(first.report, ensure_ascii=False)
            self.assertNotIn(str(root.resolve()), serialized_report)
            self.assertNotIn("file:", serialized_report)
            self.assertEqual(first.document, second.document)
            self.assertEqual(first.html_path.read_bytes(), second.html_path.read_bytes())

    def test_feature_matrix_builds_complete_evidence_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output" / "presentation.bento.html"
            result = build_from_html(
                html_dir=HTML_FIXTURES,
                registry_dir=HTML_FIXTURES,
                base_path=ROOT / "Bento_Slides.base.bento.html",
                output_path=output,
            )
            self.assertEqual(len(result.document["slides"]), 23)
            self.assertTrue(result.json_path.is_file())
            self.assertTrue(result.report_path.is_file())
            self.assertEqual(len(result.source_screenshots), 23)
            self.assertEqual(len(result.bento_screenshots), 23)
            self.assertTrue(result.report["runtimeIntegrity"])
            self.assertTrue(result.report["visualComparison"]["passed"])
            self.assertGreaterEqual(result.report["summary"]["strategies"]["svg"], 2)
            self.assertEqual(result.report["summary"]["nativeTable"], 1)
            self.assertEqual(result.report["summary"]["nativeChart"], 2)
            self.assertEqual(result.report["summary"]["nativeImage"], 1)
            self.assertEqual(result.report["summary"]["media"], 1)
            self.assertEqual(result.report["summary"]["unresolvedWarnings"], 0)
            self.assertEqual(result.report["summary"]["unresolvedLocalResourceReferences"], 0)
            self.assertGreaterEqual(result.report["summary"]["embeddedLocalAssets"], 1)
            self.assertEqual(result.report["summary"]["criticalElementFail"], 0)
            self.assertGreater(result.report["summary"]["criticalElementPass"] + result.report["summary"]["criticalElementWarning"], 0)
            self.assertTrue(result.report["resourceScan"]["passed"])
            self.assertTrue((output.parent / "diagnostics" / "resource-scan.json").is_file())
            self.assertTrue(result.report["browserCheck"]["serialize_roundtrip"])
            self.assertEqual(result.report["browserCheck"]["rendered_slide_count"], 23)
            self.assertIsInstance(result.document["assets"]["fixture-image"], str)
            image = next(element for slide in result.document["slides"] for element in slide["elements"] if element["id"] == "image-native")
            self.assertEqual(image["src"], result.document["assets"]["fixture-image"])
            state = next(slide for slide in result.document["slides"] if slide["id"] == "state-detail")
            self.assertEqual(state["stateOf"], "state-base")
            transformed = next(slide for slide in result.document["slides"] if slide["id"] == "transform-matrix")
            by_id = {element["id"]: element for element in transformed["elements"]}
            self.assertAlmostEqual(by_id["rotate-30"]["rotation"], 30, places=1)
            self.assertAlmostEqual(by_id["rotate-30"]["w"], 170, places=1)
            self.assertAlmostEqual(by_id["rotate-scale"]["w"], 229.5, places=1)
            self.assertEqual(by_id["skew-fallback"]["type"], "svg")
            computed = json.loads((output.parent / "diagnostics" / "computed-layout.json").read_text(encoding="utf-8"))
            transform_source = next(slide for slide in computed["slides"] if slide["id"] == "transform-matrix")
            transform_ids = {"rotate-30", "rotate-text-45", "origin-left-top", "rotate-scale", "translate-rotate", "skew-fallback"}
            transform_cases = {element["id"]: element for element in transform_source["elements"] if element["id"] in transform_ids}
            self.assertEqual(set(transform_cases), transform_ids)
            self.assertTrue(all(len(element["transform"]["matrix"]) == 6 for element in transform_cases.values()))
            self.assertEqual(transform_cases["origin-left-top"]["transform"]["origin"], "0px 0px")
            self.assertEqual(transform_cases["translate-rotate"]["transform"]["translateX"], 70)
            self.assertTrue(transform_cases["skew-fallback"]["transform"]["hasSkew"])
            complex_tables = next(slide for slide in result.document["slides"] if slide["id"] == "complex-tables")
            self.assertTrue(all(element["type"] == "svg" for element in complex_tables["elements"] if element["id"].endswith("-table")))
            complex_source = next(slide for slide in computed["slides"] if slide["id"] == "complex-tables")
            complex_sources = {element["id"]: element for element in complex_source["elements"] if element["type"] == "table"}
            self.assertTrue(all(not element["table"]["simpleTable"] for element in complex_sources.values()))
            sample_cell = complex_sources["colspan-table"]["table"]["rows"][0][0]
            self.assertTrue({"rowSpan", "colSpan", "rowIndex", "columnIndex", "rect", "style"}.issubset(sample_cell))
            compatibility = next(slide for slide in result.document["slides"] if slide["id"] == "css-compatibility")
            self.assertEqual(compatibility["elements"][0]["id"], "css-compatibility--background")
            self.assertEqual(compatibility["elements"][0]["type"], "shape")
            self.assertTrue(all(element["x"] >= 0 and element["y"] >= 0 and element["x"] + element["w"] <= 1280 and element["y"] + element["h"] <= 720 for slide in result.document["slides"] for element in slide["elements"]))
            portable = next(slide for slide in result.document["slides"] if slide["id"] == "portable-media-fragment")
            portable_by_id = {element["id"]: element for element in portable["elements"]}
            self.assertTrue(portable_by_id["native-video-poster"]["poster"].startswith("data:image/svg+xml;base64,"))
            self.assertTrue(portable_by_id["native-video-poster"]["poster"].endswith("#poster-root"))
            self.assertGreaterEqual(result.report["summary"]["mediaPosterEmbeddings"], 1)
            self.assertGreaterEqual(result.report["summary"]["svgFragmentPreservations"], 1)


if __name__ == "__main__":
    unittest.main()
