"""Tests for the reusable HTML/CSS + registry conversion path."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from bento_converter.bento_validator import validate_bento_doc
from bento_converter.browser_harness import BrowserHarness
from bento_converter.errors import BentoConverterError, BrowserCheckError
from bento_converter.html_converter import convert_html_layout, sanitize_inline_html
from bento_converter.html_change_review import (
    _accepted_equation_height_issue,
    _frame_issue,
    _overflow_issue,
    collect_html_change_browser_evidence,
)
from bento_converter.html_document import extract_bento_doc, write_embedded_document
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

    def test_marked_inline_math_becomes_bento_inline_latex(self) -> None:
        markup = (
            '<strong>意味：</strong>'
            '<span data-bento-inline-math="v1" class="bento-inline-math">'
            'R<sub>m</sub><sup>PR</sup></span> と '
            '<span class="bento-inline-math">κ</span>、'
            '<span data-bento-inline-math="v1">C<sub>partial</sub></span>、'
            '<span data-bento-inline-math="v1">κt<sub>R,m</sub></span>'
        )

        converted = sanitize_inline_html(markup)

        self.assertEqual(
            converted,
            '<strong>意味：</strong>$R_{m}^{\\mathrm{PR}}$ と $\\kappa$、'
            '$C_{\\mathrm{partial}}$、$\\kappa t_{R,m}$',
        )
        self.assertNotIn("bento-inline-math", converted)

    def test_inline_math_drops_unsafe_nested_markup(self) -> None:
        converted = sanitize_inline_html(
            '<span data-bento-inline-math="v1">κ<script>alert(1)</script><sup>2</sup></span>'
        )
        self.assertEqual(converted, r"$\kappa^{2}$")

    def test_table_inline_math_becomes_editable_text_overlay(self) -> None:
        table = element(
            "math-table", "table", x=100, y=120, w=600, h=100,
            table={
                "rows": [[{
                    "html": 'Candidate <span data-bento-inline-math="v1">K<sub>m</sub></span>',
                    "align": "left", "color": "#10243e", "bg": "#eef2f7", "bold": False,
                    "rect": {"x": 0, "y": 0, "w": 600, "h": 100},
                    "style": {"fontFamily": "Arial", "fontSize": 20, "fontWeight": 400},
                }]],
                "headerRows": 0,
            },
        )

        result = self.convert([table])

        output = {item["id"]: item for item in result.document["slides"][0]["elements"]}
        self.assertEqual(output["math-table"]["type"], "shape")
        self.assertEqual(output["math-table--cell-r0-c0-bg"]["type"], "shape")
        overlay = output["math-table--cell-r0-c0-text"]
        self.assertEqual(overlay["type"], "text")
        self.assertEqual(overlay["html"], r"Candidate $K_{m}$")
        self.assertEqual(overlay["valign"], "middle")
        decision = result.report["elements"][0]
        self.assertEqual(decision["strategy"], "native-decomposition")
        self.assertEqual(
            decision["emittedIds"],
            ["math-table", "math-table--cell-r0-c0-bg", "math-table--cell-r0-c0-text"],
        )

    def test_table_cell_decomposition_preserves_bottom_only_borders(self) -> None:
        bottom_border = {
            "borderWidth": 0,
            "borderColor": "rgb(24, 42, 52)",
            "borderTopWidth": 0, "borderTopColor": "rgb(24, 42, 52)", "borderTopStyle": "none",
            "borderRightWidth": 0, "borderRightColor": "rgb(24, 42, 52)", "borderRightStyle": "none",
            "borderBottomWidth": 1, "borderBottomColor": "rgb(183, 203, 199)", "borderBottomStyle": "solid",
            "borderLeftWidth": 0, "borderLeftColor": "rgb(24, 42, 52)", "borderLeftStyle": "none",
        }
        source = element(
            "comparison-table", "table", x=70, y=320, w=600, h=100,
            style={**element("unused", "table")["style"], "borderWidth": 0},
            table={
                "rows": [[{
                    "html": 'Candidate <span data-bento-inline-math="v1">K<sub>m</sub></span>',
                    "align": "left", "color": "#10243e", "bg": "transparent", "bold": False,
                    "rect": {"x": 0, "y": 0, "w": 600, "h": 100},
                    "style": {
                        "fontFamily": "Arial", "fontSize": 20, "fontWeight": 400,
                        "padding": "8px 12px", **bottom_border,
                    },
                }]],
                "headerRows": 0,
            },
        )

        result = self.convert([source])

        by_id = {item["id"]: item for item in result.document["slides"][0]["elements"]}
        background = by_id["comparison-table--cell-r0-c0-bg"]
        self.assertEqual(background["strokeWidth"], 0)
        self.assertEqual(background["stroke"], "transparent")
        self.assertIn("comparison-table--cell-r0-c0-bg--border-bottom", by_id)
        self.assertNotIn("comparison-table--cell-r0-c0-bg--border-left", by_id)
        self.assertNotIn("comparison-table--cell-r0-c0-bg--border-right", by_id)
        self.assertNotIn("comparison-table--cell-r0-c0-bg--border-top", by_id)

    def test_non_math_table_cell_decomposition_preserves_bottom_only_borders(self) -> None:
        bottom_border = {
            "borderWidth": 0,
            "borderColor": "rgb(24, 42, 52)",
            "borderTopWidth": 0, "borderTopColor": "rgb(24, 42, 52)", "borderTopStyle": "none",
            "borderRightWidth": 0, "borderRightColor": "rgb(24, 42, 52)", "borderRightStyle": "none",
            "borderBottomWidth": 1, "borderBottomColor": "rgb(183, 203, 199)", "borderBottomStyle": "solid",
            "borderLeftWidth": 0, "borderLeftColor": "rgb(24, 42, 52)", "borderLeftStyle": "none",
        }
        source = element(
            "plain-comparison-table", "table", x=70, y=320, w=600, h=100,
            style={**element("unused", "table")["style"], "borderWidth": 0},
            table={
                "rows": [[{
                    "html": "Candidate",
                    "align": "left", "color": "#10243e", "bg": "transparent", "bold": False,
                    "rect": {"x": 0, "y": 0, "w": 600, "h": 100},
                    "style": {
                        "fontFamily": "Arial", "fontSize": 20, "fontWeight": 400,
                        "padding": "8px 12px", **bottom_border,
                    },
                }]],
                "headerRows": 0,
            },
        )

        result = self.convert([source])

        by_id = {item["id"]: item for item in result.document["slides"][0]["elements"]}
        self.assertEqual(result.report["elements"][0]["strategy"], "native-decomposition")
        self.assertEqual(by_id["plain-comparison-table--cell-r0-c0-bg"]["strokeWidth"], 0)
        self.assertIn("plain-comparison-table--cell-r0-c0-bg--border-bottom", by_id)
        self.assertNotIn("plain-comparison-table--cell-r0-c0-bg--border-left", by_id)
        self.assertNotIn("plain-comparison-table--cell-r0-c0-bg--border-right", by_id)
        self.assertNotIn("plain-comparison-table--cell-r0-c0-bg--border-top", by_id)

    def test_zero_width_native_table_border_is_not_replaced_by_default_grid(self) -> None:
        source = element(
            "plain-table", "table",
            style={**element("unused", "table")["style"], "borderWidth": 0},
            table={"rows": [[{"html": "A", "style": {}}]], "headerRows": 0},
        )

        result = self.convert([source])

        table = result.document["slides"][0]["elements"][0]
        self.assertEqual(table["type"], "table")
        self.assertEqual(table["style"]["borderWidth"], 0)

    def test_single_line_text_guard_uses_measured_width(self) -> None:
        source = element(
            "chapter-label", "text", x=70, y=30, w=154, h=19,
            scrollWidth=154, scrollHeight=19,
            textLayout={"singleLine": True, "requiredWidth": 154, "maxWidth": 168},
            style={**element("unused", "text")["style"], "fontSize": 14, "lineHeight": 1.2},
        )

        result = self.convert([source])

        label = next(
            item for item in result.document["slides"][0]["elements"] if item["id"] == "chapter-label"
        )
        self.assertEqual(label["x"], 70)
        self.assertGreater(label["w"], 154)
        self.assertLess(label["w"], 158)

    def test_adjacent_single_line_text_does_not_gain_an_overlap(self) -> None:
        left = element(
            "left", "text", x=100, y=100, w=100, h=24,
            scrollWidth=100, scrollHeight=24,
            textLayout={"singleLine": True, "requiredWidth": 31, "maxWidth": 100},
            style={**element("unused", "text")["style"], "fontSize": 20, "lineHeight": 1.2},
        )
        right = element(
            "right", "text", x=200, y=100, w=100, h=24,
            scrollWidth=100, scrollHeight=24,
            textLayout={"singleLine": True, "requiredWidth": 45, "maxWidth": 1080},
            style={**element("unused", "text")["style"], "fontSize": 20, "lineHeight": 1.2},
        )

        result = self.convert([left, right])

        by_id = {item["id"]: item for item in result.document["slides"][0]["elements"]}
        self.assertEqual(by_id["left"]["w"], 100)
        self.assertEqual(by_id["right"]["w"], 100)
        self.assertEqual(result.report["summary"]["unresolvedWarnings"], 0)

    def test_owned_text_fragment_guard_stays_within_measured_boundary(self) -> None:
        panel = element(
            "panel", "shape", text="", html="",
            ownedTextFragments=[{
                "x": 214, "y": 380, "w": 46, "h": 31,
                "scrollWidth": 46, "scrollHeight": 31,
                "text": "採用", "html": '<span class="medium">採用</span>',
                "textLayout": {"singleLine": True, "requiredWidth": 46, "maxWidth": 62},
                "style": {
                    "fontSize": 23, "fontFamily": "Arial", "fontWeight": 800,
                    "lineHeight": 1.28, "letterSpacing": 0, "textAlign": "left",
                    "verticalAlign": "baseline", "display": "inline",
                    "paddingLeft": 0, "paddingRight": 0, "paddingTop": 0, "paddingBottom": 0,
                },
            }],
        )

        result = self.convert([panel])

        label = next(
            item for item in result.document["slides"][0]["elements"]
            if item["id"] == "panel--label--part-1"
        )
        self.assertGreater(label["w"], 46)
        self.assertLessEqual(label["w"], 62)

    def test_owned_text_fragment_uses_stable_bento_part_id(self) -> None:
        panel = element(
            "panel", "shape", text="", html="",
            ownedTextFragments=[{
                "partId": "heading", "x": 214, "y": 380, "w": 86, "h": 31,
                "scrollWidth": 86, "scrollHeight": 31,
                "text": "検証範囲", "html": "<span>検証範囲</span>",
                "textLayout": {"singleLine": True, "requiredWidth": 86, "maxWidth": 100},
                "style": {
                    "fontSize": 18, "fontFamily": "Arial", "fontWeight": 700,
                    "lineHeight": 1.2, "letterSpacing": 0, "textAlign": "left",
                    "verticalAlign": "baseline", "display": "block",
                    "paddingLeft": 0, "paddingRight": 0, "paddingTop": 0, "paddingBottom": 0,
                },
            }],
        )

        result = self.convert([panel])

        ids = [item["id"] for item in result.document["slides"][0]["elements"]]
        self.assertIn("panel--label--heading", ids)
        self.assertNotIn("panel--label--part-1", ids)

    def test_asymmetric_shape_borders_become_editable_edge_shapes(self) -> None:
        source = element(
            "open-panel", "shape", text="", html="", w=320, h=120,
            style={
                **element("unused", "shape")["style"],
                "backgroundColor": "transparent",
                "borderColor": "rgb(20, 30, 40) rgb(20, 30, 40) rgb(20, 30, 40) rgb(22, 134, 127)",
                "borderWidth": 0,
                "borderTopWidth": 0, "borderTopColor": "rgb(20, 30, 40)", "borderTopStyle": "none",
                "borderRightWidth": 0, "borderRightColor": "rgb(20, 30, 40)", "borderRightStyle": "none",
                "borderBottomWidth": 1, "borderBottomColor": "rgb(190, 205, 202)", "borderBottomStyle": "solid",
                "borderLeftWidth": 3, "borderLeftColor": "rgb(22, 134, 127)", "borderLeftStyle": "solid",
            },
        )

        result = self.convert([source])

        output = {item["id"]: item for item in result.document["slides"][0]["elements"]}
        self.assertEqual(output["open-panel"]["strokeWidth"], 0)
        self.assertEqual(output["open-panel--border-bottom"]["fill"], "rgb(190, 205, 202)")
        self.assertEqual(output["open-panel--border-bottom"]["h"], 1)
        self.assertEqual(output["open-panel--border-left"]["fill"], "rgb(22, 134, 127)")
        self.assertEqual(output["open-panel--border-left"]["w"], 3)
        self.assertEqual(result.report["elements"][0]["strategy"], "native-decomposition")

    def test_text_bottom_border_and_simple_pseudo_decoration_remain_native(self) -> None:
        source = element(
            "condition", "text", x=80, y=100, w=400, h=60,
            text="Condition", html="Condition",
            htmlWithPseudoReservations="<span>&nbsp;&nbsp;&nbsp;&nbsp;Condition</span>",
            pseudoDecorations=[{
                "idSuffix": "pseudo-1-before", "x": 80, "y": 104, "w": 22, "h": 3,
                "opacity": 1,
                "style": {
                    "backgroundColor": "rgb(22, 134, 127)", "borderColor": "transparent",
                    "borderWidth": 0, "borderRadius": 0,
                },
            }],
            style={
                **element("unused", "text")["style"],
                "backgroundColor": "transparent",
                "borderColor": "rgb(20, 30, 40) rgb(20, 30, 40) rgb(183, 203, 199)",
                "borderWidth": 0,
                "borderTopWidth": 0, "borderTopColor": "rgb(20, 30, 40)", "borderTopStyle": "none",
                "borderRightWidth": 0, "borderRightColor": "rgb(20, 30, 40)", "borderRightStyle": "none",
                "borderBottomWidth": 1, "borderBottomColor": "rgb(183, 203, 199)", "borderBottomStyle": "solid",
                "borderLeftWidth": 0, "borderLeftColor": "rgb(20, 30, 40)", "borderLeftStyle": "none",
            },
        )

        result = self.convert([source])

        output = {item["id"]: item for item in result.document["slides"][0]["elements"]}
        self.assertEqual(output["condition--decoration--border-bottom"]["h"], 1)
        self.assertEqual(output["condition--pseudo-1-before"]["fill"], "rgb(22, 134, 127)")
        self.assertEqual(output["condition"]["type"], "text")
        self.assertIn("&nbsp;&nbsp;", output["condition"]["html"])
        self.assertEqual(result.report["elements"][0]["strategy"], "native-decomposition")
    def test_asymmetric_cell_borders_preserve_native_text_and_measured_height(self) -> None:
        borders = {
            f"border{side.title()}{field}": value
            for side in ("top", "right", "bottom", "left")
            for field, value in (("Width", 0), ("Color", "#ccc"), ("Style", "none"))
        }
        borders.update({"borderBottomWidth": 1, "borderBottomStyle": "solid"})
        source = element("table", "table", table={"rows": [[{
            "html": "H6", "rect": {"x": 0, "y": 0, "w": 54, "h": 52},
            "style": {"padding": "10px 12px", "fontSize": 22, "lineHeight": 1.35, **borders},
        }]], "headerRows": 0})
        result = self.convert([source])
        output = {e["id"]: e for e in result.document["slides"][0]["elements"]}
        self.assertEqual(output["table"]["type"], "shape")
        self.assertEqual(output["table--cell-r0-c0-bg"]["strokeWidth"], 0)
        self.assertEqual(output["table--cell-r0-c0-bg"]["fill"], "transparent")
        self.assertEqual(output["table--cell-r0-c0-bg--border-bottom"]["shape"], "rect")
        self.assertNotIn("table--cell-r0-c0-bg--border-top", output)
        self.assertEqual(output["table--cell-r0-c0-text"]["html"], "H6")
        self.assertEqual(output["table--cell-r0-c0-text"]["h"], 52)
        self.assertEqual(output["table--cell-r0-c0-text"]["lineHeight"], 1.35)

    def test_measured_single_line_label_does_not_gain_a_line_break(self) -> None:
        source = element("page", "text", html="06 / 46", text="06 / 46", w=59, h=22,
                         scrollWidth=59, scrollHeight=22,
                         style={**element("unused", "text")["style"], "fontSize": 18, "lineHeight": 1.2})
        result = self.convert([source])
        output = next(e for e in result.document["slides"][0]["elements"] if e["id"] == "page")
        self.assertEqual(output["html"], '06 / 46')
        self.assertEqual(output["w"], 60)
        self.assertEqual(output["h"], 22)

    def test_table_preserves_different_uniform_borders_between_cells(self) -> None:
        cells = []
        for width, color in ((1, "#112233"), (2, "#335577")):
            style = {
                f"border{side.title()}{field}": value
                for side in ("top", "right", "bottom", "left")
                for field, value in (("Width", width), ("Color", color), ("Style", "solid"))
            }
            cells.append({"html": "Cell", "style": style, "bg": "transparent"})
        source = element("table", "table", table={"rows": [cells], "headerRows": 0})

        result = self.convert([source])

        by_id = {item["id"]: item for item in result.document["slides"][0]["elements"]}
        self.assertEqual(by_id["table"]["type"], "shape")
        self.assertEqual(by_id["table--cell-r0-c0-bg"]["strokeWidth"], 1)
        self.assertEqual(by_id["table--cell-r0-c1-bg"]["strokeWidth"], 2)
        self.assertEqual(by_id["table--cell-r0-c1-bg"]["stroke"], "#335577")

    def test_thin_dashed_and_vertical_dotted_cell_edges_keep_gaps_and_bounds(self) -> None:
        style = {
            f"border{side.title()}{field}": value
            for side in ("top", "right", "bottom", "left")
            for field, value in (("Width", 0), ("Color", "#112233"), ("Style", "none"))
        }
        style.update({"borderBottomWidth": 1, "borderBottomStyle": "dashed",
                      "borderRightWidth": 2, "borderRightStyle": "dotted"})
        source = element("table", "table", x=100, y=200, w=100, h=52,
                         table={"rows": [[{"html": "Cell", "bg": "transparent", "style": style,
                                           "rect": {"x": 0, "y": 0, "w": 100, "h": 52}}]], "headerRows": 0})

        result = self.convert([source])

        output = result.document["slides"][0]["elements"]
        bottom = [item for item in output if "--border-bottom" in item["id"]]
        right = [item for item in output if "--border-right" in item["id"]]
        self.assertGreater(len(bottom), 1)
        self.assertGreater(len(right), 1)
        self.assertTrue(all(item["shape"] == "rect" and item["h"] == 1 and item["y"] == 251 for item in bottom))
        self.assertTrue(all(item["shape"] == "ellipse" and item["w"] == 2 and item["x"] == 198 for item in right))
        self.assertTrue(all(item["x"] + item["w"] <= 200 for item in bottom))
        self.assertTrue(all(item["y"] + item["h"] <= 252 for item in right))
        self.assertGreater(bottom[1]["x"], bottom[0]["x"] + bottom[0]["w"])
        self.assertGreater(right[1]["y"], right[0]["y"] + right[0]["h"])

    def test_single_line_fallback_does_not_extend_past_canvas(self) -> None:
        source = element("edge", "text", x=1279, y=10, w=1, h=22,
                         scrollWidth=1, scrollHeight=22,
                         style={**element("unused", "text")["style"], "whiteSpace": "nowrap"})

        result = self.convert([source])

        output = next(item for item in result.document["slides"][0]["elements"] if item["id"] == "edge")
        self.assertEqual(output["x"] + output["w"], 1280)

    def test_background_fallback_keeps_owned_and_descendant_text_editable(self) -> None:
        parent = element(
            "card", "shape", text="Protected label", html="Protected label",
            residualText="Card heading", residualHtml="Card heading",
            hasExplicitDescendants=True, explicitDescendantIds=["card-label"],
            fallbackScope="background",
            fallbackCapture={
                "scope": "background", "excludedElementIds": ["card-label"], "ownedTextExcluded": True,
            },
            style={**element("unused", "shape")["style"], "filter": "blur(2px)"},
        )
        child = element(
            "card-label", "text", domIndex=1, z=1, text="Protected label", html="Protected label",
            ancestorIds=["card"],
        )
        layout = LayoutResult(({
            "id": "slide", "background": "rgb(255, 255, 255)", "transition": "none", "notes": "note",
            "layout": "free", "elements": [parent, child],
        },), (), {"slide/card": "data:image/png;base64,AAAA"}, "test Chromium")

        result = convert_html_layout(layout, self.registry, [self.chapter])

        output = result.document["slides"][0]["elements"]
        self.assertEqual(
            [item["id"] for item in output],
            ["card", "card--label", "card-label--decoration", "card-label"],
        )
        self.assertEqual(output[0]["type"], "image")
        self.assertNotIn("sourceText", output[0])
        self.assertEqual(output[1]["type"], "text")
        self.assertEqual(output[-1]["type"], "text")
        decisions = {item["elementId"]: item for item in result.report["elements"]}
        self.assertEqual(decisions["card"]["strategy"], "background-image")
        self.assertEqual(decisions["card"]["editableOverlayIds"], ["card--label"])
        self.assertEqual(decisions["card-label"]["strategy"], "native-decomposition")
        self.assertEqual(result.report["summary"]["bakedTextCount"], 0)
        self.assertEqual(result.report["summary"]["coveredEditableElementCount"], 0)
        validation = validate_bento_doc(result.document)
        self.assertEqual(validation.warnings, ())

    def test_shape_label_contrast_uses_the_parent_shape_fill(self) -> None:
        source = element(
            "summary", "shape", text="Readable on dark fill", html="Readable on dark fill",
            style={
                **element("unused", "shape")["style"],
                "color": "rgb(255, 255, 255)",
                "backgroundColor": "rgb(33, 56, 53)",
            },
        )

        result = self.convert([source])

        by_id = {item["id"]: item for item in result.document["slides"][0]["elements"]}
        self.assertEqual(by_id["summary--label"]["color"], "rgb(255, 255, 255)")
        self.assertFalse(any(
            item["kind"] == "low-contrast" and item["elementId"] == "summary--label"
            for item in result.report["corrections"]
        ))

    def test_unsupported_equation_keeps_an_editable_equation_overlay(self) -> None:
        source = element(
            "equation", "equation", equationId="eq", text="x squared", html="x squared",
            style={**element("unused", "equation")["style"], "filter": "blur(2px)"},
        )
        layout = LayoutResult(({
            "id": "slide", "background": "rgb(255, 255, 255)", "transition": "none", "notes": "",
            "layout": "free", "elements": [source],
        },), (), {"slide/equation": "data:image/png;base64,AAAA"}, "test Chromium")

        result = convert_html_layout(layout, self.registry, [self.chapter])

        by_id = {item["id"]: item for item in result.document["slides"][0]["elements"]}
        self.assertEqual(by_id["equation"]["type"], "image")
        self.assertEqual(by_id["equation--label"]["type"], "text")
        self.assertEqual(by_id["equation--label"]["equationId"], "eq")
        self.assertEqual(by_id["equation--label"]["latexSource"], "x^2")
        self.assertEqual(by_id["equation--label"]["html"], "$$x^2$$")
        decision = result.report["elements"][0]
        self.assertEqual(decision["strategy"], "background-image")
        self.assertEqual(decision["editableOverlayIds"], ["equation--label"])
        self.assertEqual(result.report["summary"]["bakedEquationCount"], 0)

    def test_svg_equation_is_reported_as_baked_content(self) -> None:
        result = self.convert([element("equation", "equation", equationId="eq", exportMode="svg")])

        self.assertEqual(result.report["elements"][0]["strategy"], "svg")
        self.assertEqual(result.report["summary"]["bakedEquationCount"], 1)

    def test_flex_text_children_keep_separate_native_positions(self) -> None:
        source = element(
            "footer", "text", text="PR+DF コスト評価 15", html="<span>PR+DF コスト評価</span><span>15</span>",
            textFragments=[
                {"x": 64, "y": 680, "w": 300, "h": 20, "scrollWidth": 300, "scrollHeight": 20,
                 "text": "PR+DF コスト評価", "html": "PR+DF コスト評価", "style": {}},
                {"x": 1170, "y": 680, "w": 46, "h": 20, "scrollWidth": 46, "scrollHeight": 20,
                 "text": "15", "html": "15", "style": {"textAlign": "right"}},
            ],
            style={**element("unused", "text")["style"], "display": "flex", "justifyContent": "space-between"},
        )
        result = self.convert([source])
        output = result.document["slides"][0]["elements"]
        parts = {item["id"]: item for item in output}
        self.assertNotIn("footer", parts)
        self.assertEqual(parts["footer--part-1"]["x"], 64)
        self.assertEqual(parts["footer--part-2"]["x"], 1170)
        self.assertEqual(parts["footer--part-2"]["align"], "right")

    def test_native_text_equation_shape_table_chart_svg_and_media(self) -> None:
        values = [
            element("text", "text", html="<strong>Safe</strong><script>bad()</script>"),
            element("eq", "equation", equationId="eq"),
            element("shape", "shape", shape="rounded", text="", html=""),
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
        raster = element("raster", "complex", exportMode="image", text="", html="")
        ignored = element("guide", "text", exportMode="ignore", domIndex=1)
        layout = LayoutResult(({
            "id": "slide", "background": "rgb(255, 255, 255)", "transition": "none", "notes": "",
            "layout": "custom", "elements": [raster, ignored],
        },), (), {"slide/raster": "data:image/png;base64,AAAA"}, "test Chromium")
        result = convert_html_layout(layout, self.registry, [self.chapter])
        self.assertEqual([item["type"] for item in result.document["slides"][0]["elements"]], ["image"])
        self.assertEqual(result.report["summary"]["imageFallback"], 1)
        self.assertEqual(result.report["summary"]["strategies"]["ignore"], 1)

    def test_post_apply_bounds_use_the_visible_rotated_bounding_frame(self) -> None:
        issue = _frame_issue(element(
            "rotated", "shape", x=1100, y=100, w=180, h=180,
            boundingFrame={"x": 1062, "y": 62, "w": 255, "h": 255},
        ))
        self.assertIsNotNone(issue)
        self.assertEqual(issue["kind"], "element-out-of-bounds")
        self.assertEqual(issue["frame"]["x"], 1062.0)
        self.assertEqual(issue["geometryFrame"]["x"], 1100.0)

    def test_post_apply_overflow_checks_text_bearing_shapes(self) -> None:
        issue = _overflow_issue(element(
            "shape-label", "shape", text="A clipped label",
            w=100, h=30, clientWidth=100, clientHeight=30,
            scrollWidth=180, scrollHeight=30,
        ))
        self.assertIsNotNone(issue)
        self.assertEqual(issue["kind"], "element-content-overflow")

    def test_height_acceptance_is_exact_and_does_not_accept_other_failures(self) -> None:
        equation = element("formula", "equation", clientWidth=200, clientHeight=30,
                           scrollWidth=200, scrollHeight=34)
        issue = _overflow_issue(equation)
        accepted = [{"slideId": "slide", **issue}]
        self.assertTrue(_accepted_equation_height_issue("slide", equation, issue, accepted))
        self.assertFalse(_accepted_equation_height_issue("another", equation, issue, accepted))
        self.assertFalse(_accepted_equation_height_issue("slide", {**equation, "type": "text"}, issue, accepted))
        self.assertFalse(_accepted_equation_height_issue("slide", equation, {**issue, "scroll": {"w": 200, "h": 35}}, accepted))
        width_issue = {**issue, "scroll": {"w": 205, "h": 34}}
        self.assertFalse(_accepted_equation_height_issue("slide", equation, width_issue, [{"slideId": "slide", **width_issue}]))
        bounds_issue = {"kind": "element-out-of-bounds", "elementId": "formula"}
        self.assertFalse(_accepted_equation_height_issue("slide", equation, bounds_issue, [{"slideId": "slide", **bounds_issue}]))

    def test_explicit_native_still_falls_back_for_impossible_css(self) -> None:
        unsafe = element(
            "clipped", "text", exportMode="native",
            style={**element("unused", "text")["style"], "clipPath": "polygon(0 0,100% 0,50% 100%)"},
        )
        layout = LayoutResult(({
            "id": "slide", "background": "rgb(255, 255, 255)", "transition": "none", "notes": "",
            "layout": "free", "elements": [unsafe],
        },), (), {"slide/clipped": "data:image/png;base64,AAAA"}, "test Chromium")
        result = convert_html_layout(layout, self.registry, [self.chapter])
        output = result.document["slides"][0]["elements"]
        decision = result.report["elements"][0]
        self.assertEqual([item["type"] for item in output], ["image", "text"])
        self.assertEqual(decision["strategy"], "background-image")
        self.assertEqual(decision["nativeCompatibility"], "background-image-required")
        self.assertIn("clip-path", " ".join(decision["compatibilityReasons"]))

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
    def test_measured_table_cell_styles_and_patterned_edges_survive_serialize(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "cell-styles.preview.html"
            registry_path = root / "cell-styles.registry.json"
            output = root / "output/presentation.generated.bento.html"
            source.write_text(
                "<!doctype html><style>html,body{margin:0}.slide{position:relative;width:1280px;height:720px;"
                "background:#f4f7fb}table{position:absolute;left:100px;top:200px;width:240px;border-spacing:0;"
                "border-collapse:separate;font:18px/1.2 Arial}td{height:52px;box-sizing:border-box;padding:8px 12px;"
                "background:transparent;border:0;border-bottom:1px dashed #112233;"
                "border-right:2px dotted #112233;line-height:1.35}</style>"
                "<section class='slide' data-slide-id='cell-styles'><table data-bento-id='table' "
                "data-bento-type='table'><tbody><tr><td>Measured cell</td></tr></tbody></table></section>",
                encoding="utf-8",
            )
            registry_path.write_text(json.dumps({
                "format": "bento/html-registry/v2", "unitId": "cell-styles", "sources": {},
                "document": {"title": "Cell styles"}, "assets": {}, "fonts": {},
                "equations": {}, "figures": {}, "tables": {}, "charts": {},
                "protected": {"slideIds": [], "elementIds": [], "requiredText": []},
            }), encoding="utf-8")
            result = build_from_html(
                html_path=source, registry_path=registry_path,
                base_path=ROOT / "Bento_Slides.base.bento.html", output_path=output,
            )
            by_id = {item["id"]: item for item in result.document["slides"][0]["elements"]}
            self.assertEqual(by_id["table--cell-r0-c0-bg"]["fill"], "rgba(0, 0, 0, 0)")
            self.assertAlmostEqual(by_id["table--cell-r0-c0-text"]["lineHeight"], 1.35)
            self.assertEqual(by_id["table--cell-r0-c0-bg--border-bottom"]["h"], 1)
            self.assertEqual(by_id["table--cell-r0-c0-bg--border-right"]["w"], 2)
            with BrowserHarness() as harness:
                with harness.page("bentoCheck") as page:
                    page.goto(output.resolve().as_uri(), wait_until="load")
                    page.wait_for_selector('.ed-stage [data-el-id="table--cell-r0-c0-text"]')
                    harness.settle(page)
                    visible_patterns = page.evaluate("""() => ({
                        dashes: document.querySelectorAll('.ed-stage [data-el-id*="--border-bottom"] rect').length,
                        dots: document.querySelectorAll('.ed-stage [data-el-id*="--border-right"] ellipse').length,
                        serialized: window.bento.serialize()
                    })""")
                    self.assertGreater(visible_patterns["dashes"], 1)
                    self.assertGreater(visible_patterns["dots"], 1)
                    saved = extract_bento_doc(visible_patterns["serialized"])
                    self.assertEqual(saved["slides"], result.document["slides"])

    def test_block_heading_with_direct_body_text_becomes_separate_editable_labels(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "block-heading.preview.html"
            registry_path = root / "block-heading.registry.json"
            output = root / "output/presentation.generated.bento.html"
            source.write_text(
                "<!doctype html><style>html,body{margin:0}.slide{position:relative;width:1280px;height:720px;"
                "background:#fff}.panel{position:absolute;left:100px;top:100px;width:520px;height:150px;"
                "padding:12px;box-sizing:border-box;font:400 17px/1.35 Arial;color:#263b44}.panel-title{display:block;"
                "margin:0 0 7px;font-size:13px;line-height:1.35;font-weight:800;color:#16867f}.panel-title:before{"
                "content:'';display:inline-block;width:22px;height:3px;margin:0 9px 3px 0;background:#16867f}"
                ".small{font-size:14px;line-height:1.5;color:#647478}</style>"
                "<section class='slide' data-slide-id='block-heading'><div class='panel' data-bento-id='panel' "
                "data-bento-type='shape'><span class='panel-title' data-bento-part='heading'>今回の検証範囲</span>"
                "条件を指定して直接生成<br><span class='small'>各"
                "<span data-bento-inline-math='v1'>L</span>で500</span></div></section>",
                encoding="utf-8",
            )
            registry_path.write_text(json.dumps({
                "format": "bento/html-registry/v2", "unitId": "block-heading", "sources": {},
                "document": {"title": "Block heading"}, "assets": {}, "fonts": {},
                "equations": {}, "figures": {}, "tables": {}, "charts": {},
                "protected": {"slideIds": [], "elementIds": [], "requiredText": []},
            }), encoding="utf-8")

            result = build_from_html(
                html_path=source, registry_path=registry_path,
                base_path=ROOT / "Bento_Slides.base.bento.html", output_path=output,
            )
            by_id = {item["id"]: item for item in result.document["slides"][0]["elements"]}
            heading = by_id["panel--label--heading"]
            body = by_id["panel--label--part-2"]
            self.assertEqual(heading["html"], '<span class="panel-title">今回の検証範囲</span>')
            self.assertNotIn("条件を指定", heading["html"])
            self.assertIn("条件を指定して直接生成<br>", body["html"])
            self.assertIn("各$L$で500", body["html"])
            self.assertNotIn("今回の検証範囲", body["html"])
            self.assertGreaterEqual(body["y"], heading["y"] + heading["h"] + 5)
            self.assertEqual(heading["fontSize"], 13)
            self.assertEqual(body["fontSize"], 17)
            computed = json.loads((output.parent / "diagnostics/computed-layout.json").read_text(encoding="utf-8"))
            source_panel = next(
                item for item in computed["slides"][0]["elements"] if item["id"] == "panel"
            )
            source_heading = source_panel["ownedTextFragments"][0]
            self.assertEqual(source_heading["partId"], "heading")
            self.assertEqual(source_heading["textLayout"]["lineCount"], 1)
            self.assertEqual(len(source_heading["textLayout"]["lines"]), 1)

            with BrowserHarness() as harness:
                with harness.page("bentoCheck") as page:
                    page.goto(output.resolve().as_uri(), wait_until="load")
                    page.wait_for_selector('.ed-stage [data-el-id="panel--label--part-2"]')
                    harness.settle(page)
                    positions = page.evaluate(
                        """() => {
                          const heading = document.querySelector('.ed-stage [data-el-id="panel--label--heading"]');
                          const body = document.querySelector('.ed-stage [data-el-id="panel--label--part-2"]');
                          const textRect = (root, needle) => {
                            const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
                            while (walker.nextNode()) {
                              const node = walker.currentNode;
                              const index = node.textContent.indexOf(needle);
                              if (index >= 0) {
                                const range = document.createRange();
                                range.setStart(node, index); range.setEnd(node, index + needle.length);
                                return range.getBoundingClientRect();
                              }
                            }
                            return null;
                          };
                          const headingText = textRect(heading, '今回の検証範囲');
                          const firstBody = textRect(body, '条件を指定して直接生成');
                          const secondBody = textRect(body, '各');
                          return {
                            headingBottom: headingText.bottom,
                            firstBodyTop: firstBody.top,
                            firstBodyBottom: firstBody.bottom,
                            secondBodyTop: secondBody.top,
                          };
                        }"""
                    )
                    self.assertGreaterEqual(positions["firstBodyTop"], positions["headingBottom"] + 5)
                    self.assertGreater(positions["secondBodyTop"], positions["firstBodyBottom"])

    def test_inline_pseudo_decoration_keeps_its_text_advance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "pseudo.preview.html"
            registry_path = root / "pseudo.registry.json"
            output = root / "output/presentation.generated.bento.html"
            source.write_text(
                "<!doctype html><style>html,body{margin:0}.slide{position:relative;width:1280px;height:720px;"
                "background:#fff}.panel{position:absolute;left:100px;top:100px;width:420px;height:120px;"
                "padding:8px;box-sizing:border-box;font:400 18px Arial}.panel-title{display:block;margin:0 0 8px;"
                "font-size:13px;font-weight:800;color:#16867f}.medium{font-size:23px;font-weight:800}.small{font-size:14px}"
                ".panel-title:before{content:'';display:inline-block;width:22px;height:3px;margin:0 9px 3px 0;"
                "background:#16867f}.hidden{top:260px}.hidden .panel-title:before{opacity:0}</style>"
                "<section class='slide' data-slide-id='pseudo'><div class='panel' data-bento-id='panel' "
                "data-bento-type='shape'><span class='panel-title'>Heading</span><span class='medium'>Metric</span>"
                "<br><span class='small'>Second</span></div><div class='panel hidden' data-bento-id='hidden-panel' "
                "data-bento-type='shape'><span class='panel-title'>Hidden decoration</span></div></section>",
                encoding="utf-8",
            )
            registry_path.write_text(json.dumps({
                "format": "bento/html-registry/v2", "unitId": "pseudo", "sources": {},
                "document": {"title": "Pseudo spacing"}, "assets": {}, "fonts": {},
                "equations": {}, "figures": {}, "tables": {}, "charts": {},
                "protected": {"slideIds": [], "elementIds": [], "requiredText": []},
            }), encoding="utf-8")

            result = build_from_html(
                html_path=source, registry_path=registry_path,
                base_path=ROOT / "Bento_Slides.base.bento.html", output_path=output,
            )
            by_id = {item["id"]: item for item in result.document["slides"][0]["elements"]}
            self.assertIn("panel--pseudo-2-before", by_id)
            self.assertNotIn("hidden-panel--pseudo-2-before", by_id)
            self.assertEqual(
                [by_id[f"panel--label--part-{index}"]["fontSize"] for index in (1, 2, 3)],
                [13, 23, 14],
            )

            with BrowserHarness() as harness:
                with harness.page("bentoCheck") as page:
                    page.goto(output.resolve().as_uri(), wait_until="load")
                    page.wait_for_selector(".ed-root")
                    harness.settle(page)
                    positions = page.evaluate(
                        """() => {
                          const line = document.querySelector('.ed-stage [data-el-id="panel--pseudo-2-before"]')
                            .getBoundingClientRect();
                          const label = document.querySelector('.ed-stage [data-el-id="panel--label--part-1"]');
                          const metricLabel = document.querySelector('.ed-stage [data-el-id="panel--label--part-2"]');
                          const smallLabel = document.querySelector('.ed-stage [data-el-id="panel--label--part-3"]');
                          const locate = needle => {
                            for (const root of [label, metricLabel, smallLabel]) {
                              const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
                              while (walker.nextNode()) {
                                const node = walker.currentNode;
                                const index = node.textContent.indexOf(needle);
                                if (index >= 0) {
                                  const range = document.createRange();
                                  range.setStart(node, index); range.setEnd(node, index + 1);
                                  return range.getBoundingClientRect();
                                }
                              }
                            }
                            return null;
                          };
                          const first = locate('Heading');
                          const medium = locate('Metric');
                          const second = locate('Second');
                          const styleFor = needle => {
                            for (const root of [label, metricLabel, smallLabel]) {
                              const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
                              while (walker.nextNode()) {
                                if (walker.currentNode.textContent.includes(needle)) {
                                  const style = getComputedStyle(walker.currentNode.parentElement);
                                  return {display: style.display, fontSize: style.fontSize};
                                }
                              }
                            }
                            return null;
                          };
                          return {lineRight: line.right, firstLeft: first.left, firstBottom: first.bottom,
                            mediumTop: medium.top, secondLeft: second.left,
                            labelLeft: smallLabel.getBoundingClientRect().left,
                            headingStyle: styleFor('Heading'), mediumStyle: styleFor('Metric'),
                            smallStyle: styleFor('Second')};
                        }"""
                    )
                    self.assertGreaterEqual(positions["firstLeft"], positions["lineRight"] + 7)
                    self.assertLessEqual(positions["firstLeft"], positions["lineRight"] + 24)
                    self.assertGreaterEqual(
                        positions["mediumTop"], positions["firstBottom"] + 5.5, positions,
                    )
                    self.assertAlmostEqual(positions["secondLeft"], positions["labelLeft"], delta=2)
                    self.assertEqual(positions["headingStyle"]["fontSize"], "13px")
                    self.assertEqual(positions["mediumStyle"]["fontSize"], "23px")
                    self.assertEqual(positions["smallStyle"]["fontSize"], "14px")

    def test_adjacent_single_line_html_text_does_not_gain_an_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "adjacent.preview.html"
            registry_path = root / "adjacent.registry.json"
            output = root / "output/presentation.generated.bento.html"
            source.write_text(
                "<!doctype html><style>html,body{margin:0}.slide{position:relative;width:1280px;height:720px;"
                "background:#fff}.text{position:absolute;top:100px;width:100px;height:24px;"
                "font:400 20px/1.2 Arial}</style><section class='slide' data-slide-id='adjacent'>"
                "<div class='text' style='left:100px' data-bento-id='left' data-bento-type='text'>Left</div>"
                "<div class='text' style='left:200px' data-bento-id='right' data-bento-type='text'>Right</div>"
                "</section>",
                encoding="utf-8",
            )
            registry_path.write_text(json.dumps({
                "format": "bento/html-registry/v2", "unitId": "adjacent", "sources": {},
                "document": {"title": "Adjacent text"}, "assets": {}, "fonts": {},
                "equations": {}, "figures": {}, "tables": {}, "charts": {},
                "protected": {"slideIds": [], "elementIds": [], "requiredText": []},
            }), encoding="utf-8")

            result = build_from_html(
                html_path=source, registry_path=registry_path,
                base_path=ROOT / "Bento_Slides.base.bento.html", output_path=output,
                browser_check=False,
            )

            by_id = {item["id"]: item for item in result.document["slides"][0]["elements"]}
            self.assertEqual(by_id["left"]["w"], 100)
            self.assertEqual(by_id["right"]["w"], 100)
            self.assertEqual(result.report["summary"]["unresolvedWarnings"], 0)

    def test_table_inline_math_renders_without_visible_delimiters_and_survives_serialize(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output/presentation.generated.bento.html"
            source_element = element(
                "math-table", "table", x=100, y=120, w=600, h=100,
                table={
                    "rows": [[{
                        "html": (
                            'Candidate <span data-bento-inline-math="v1">K<sub>m</sub></span> and '
                            '<span data-bento-inline-math="v1">&#954;</span>'
                        ),
                        "align": "left", "color": "#10243e", "bg": "#eef2f7", "bold": False,
                        "rect": {"x": 0, "y": 0, "w": 600, "h": 100},
                        "style": {"fontFamily": "Arial", "fontSize": 20, "fontWeight": 400},
                    }]],
                    "headerRows": 0,
                },
            )
            layout = LayoutResult(({
                "id": "table-math", "background": "rgb(255, 255, 255)", "transition": "none",
                "notes": "", "layout": "free", "elements": [source_element],
            },), (), {}, "test Chromium")
            registry = {
                "document": {"title": "Table inline math"}, "assets": {}, "fonts": {},
                "equations": {}, "figures": {}, "tables": {}, "charts": {},
                "protected": {"slideIds": [], "elementIds": [], "requiredText": []},
            }
            chapter = SourceChapter("table-math", root / "table.html", root / "table.json", {})
            result = convert_html_layout(layout, registry, [chapter])
            write_embedded_document(ROOT / "Bento_Slides.base.bento.html", output, result.document)

            with BrowserHarness() as harness:
                with harness.page("bentoCheck") as page:
                    page.goto(output.resolve().as_uri(), wait_until="load")
                    page.wait_for_selector(".ed-root")
                    harness.settle(page)
                    overlay = page.locator(
                        '.ed-stage [data-el-id="math-table--cell-r0-c0-text"]'
                    )
                    self.assertIn(
                        "bento-el-shape",
                        page.locator('.ed-stage [data-el-id="math-table"]').get_attribute("class") or "",
                    )
                    self.assertIn(
                        "bento-el-shape",
                        page.locator('.ed-stage [data-el-id="math-table--cell-r0-c0-bg"]').get_attribute("class") or "",
                    )
                    self.assertEqual(overlay.locator("math").count(), 2)
                    self.assertNotIn("$", overlay.text_content() or "")
                    bounds = page.evaluate(
                        """() => {
                          const text = document.querySelector('.ed-stage [data-el-id="math-table--cell-r0-c0-text"]')
                            .getBoundingClientRect();
                          const cell = document.querySelector('.ed-stage [data-el-id="math-table--cell-r0-c0-bg"]')
                            .getBoundingClientRect();
                          return {
                            left: text.left >= cell.left - 1,
                            top: text.top >= cell.top - 1,
                            right: text.right <= cell.right + 1,
                            bottom: text.bottom <= cell.bottom + 1,
                          };
                        }"""
                    )
                    self.assertTrue(all(bounds.values()))
                    serialized = page.evaluate("window.bento.serialize()")
                    serialized_doc = extract_bento_doc(serialized)
                    elements = {item["id"]: item for item in serialized_doc["slides"][0]["elements"]}
                    self.assertEqual(elements["math-table"]["type"], "shape")
                    self.assertEqual(
                        elements["math-table--cell-r0-c0-text"]["html"],
                        r"Candidate $K_{m}$ and $\kappa$",
                    )

    def test_inline_math_renders_as_math_and_survives_serialize(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output/presentation.generated.bento.html"
            source_element = element(
                "inline-title", "text", x=100, y=100, w=900, h=100,
                text="RmPR と κ の意味",
                html=(
                    "<span data-bento-inline-math='v1'>R<sub>m</sub><sup>PR</sup></span> と "
                    "<span data-bento-inline-math='v1'>κ</span> の意味"
                ),
            )
            layout = LayoutResult(({
                "id": "inline", "background": "rgb(255, 255, 255)", "transition": "none",
                "notes": "", "layout": "free", "elements": [source_element],
            },), (), {}, "test Chromium")
            registry = {
                "document": {"title": "Inline math"}, "assets": {}, "fonts": {},
                "equations": {}, "figures": {}, "tables": {}, "charts": {},
                "protected": {"slideIds": [], "elementIds": [], "requiredText": []},
            }
            chapter = SourceChapter("inline", root / "inline.html", root / "inline.json", {})
            result = convert_html_layout(layout, registry, [chapter])
            write_embedded_document(ROOT / "Bento_Slides.base.bento.html", output, result.document)

            inline = result.document["slides"][0]["elements"][-1]
            self.assertEqual(inline["id"], "inline-title")
            self.assertEqual(inline["html"], r"$R_{m}^{\mathrm{PR}}$ と $\kappa$ の意味")
            with BrowserHarness() as harness:
                with harness.page("bentoCheck") as page:
                    page.goto(output.resolve().as_uri(), wait_until="load")
                    page.wait_for_selector(".ed-root")
                    harness.settle(page)
                    rendered = page.locator('.ed-stage [data-el-id="inline-title"]')
                    self.assertEqual(rendered.locator("math").count(), 2)
                    serialized = page.evaluate("window.bento.serialize()")
                    self.assertIsInstance(serialized, str)
                    serialized_doc = extract_bento_doc(serialized)
                    serialized_inline = serialized_doc["slides"][0]["elements"][-1]
                    self.assertEqual(
                        serialized_inline["html"],
                        r"$R_{m}^{\mathrm{PR}}$ と $\kappa$ の意味",
                    )

    def test_complex_background_keeps_text_and_equation_native(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "editable.preview.html"
            registry_path = root / "editable.registry.json"
            source.write_text(
                "<!doctype html><style>body{margin:0}.slide{position:relative;width:1280px;height:720px;background:#fff}"
                ".card{position:absolute;left:180px;top:120px;width:700px;height:360px;padding:32px;"
                "box-sizing:border-box;background:#fee2e2;border:3px solid #b91c1c;filter:drop-shadow(0 12px 8px #999);"
                "font:700 30px Arial;color:#111}.eq{position:absolute;left:70px;top:130px;width:400px;height:90px;"
                "font:400 28px Arial;color:#111}</style>"
                "<section class='slide' data-slide-id='editable'>"
                "<div class='card' data-bento-id='card' data-bento-type='shape'>Card heading"
                "<div class='eq' data-bento-id='equation' data-bento-type='equation' data-equation-id='eq'>x²</div>"
                "</div></section>",
                encoding="utf-8",
            )
            registry_path.write_text(json.dumps({
                "format": "bento/html-registry/v2", "unitId": "editable", "sources": {},
                "document": {"title": "Editable fallback"}, "assets": {}, "fonts": {},
                "equations": {"eq": {"latex": "x^2"}}, "figures": {}, "tables": {}, "charts": {},
                "protected": {"slideIds": [], "elementIds": ["equation"], "requiredText": []},
            }), encoding="utf-8")

            result = build_from_html(
                html_path=source, registry_path=registry_path,
                base_path=ROOT / "Bento_Slides.base.bento.html",
                output_path=root / "output/presentation.generated.bento.html",
            )

            by_id = {item["id"]: item for item in result.document["slides"][0]["elements"]}
            self.assertEqual(by_id["card"]["type"], "image")
            self.assertNotIn("sourceText", by_id["card"])
            self.assertEqual(by_id["card--label"]["type"], "text")
            self.assertEqual(by_id["equation"]["type"], "text")
            self.assertEqual(by_id["equation"]["latexSource"], "x^2")
            self.assertEqual(result.report["summary"]["backgroundImageWithNativeOverlay"], 1)
            self.assertEqual(result.report["summary"]["bakedTextCount"], 0)
            self.assertEqual(result.report["summary"]["bakedEquationCount"], 0)
            self.assertEqual(result.report["summary"]["coveredEditableElementCount"], 0)
            self.assertTrue(result.report["browserCheck"]["serialize_roundtrip"])

    def test_styled_equation_uses_background_capture_with_editable_math(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "equation.preview.html"
            registry_path = root / "equation.registry.json"
            output = root / "output/presentation.generated.bento.html"
            source.write_text(
                "<!doctype html><style>html,body{margin:0}.slide{position:relative;width:1280px;height:720px;"
                "background:#fff}.equation{position:absolute;left:200px;top:180px;width:700px;height:120px;"
                "font:400 34px Arial;color:#10243e;filter:drop-shadow(0 8px 5px #94a3b8)}</style>"
                "<section class='slide' data-slide-id='equation-slide'>"
                "<div class='equation' data-bento-id='equation' data-bento-type='equation' "
                "data-equation-id='eq'>x²</div></section>",
                encoding="utf-8",
            )
            registry_path.write_text(json.dumps({
                "format": "bento/html-registry/v2", "unitId": "equation-slide", "sources": {},
                "document": {"title": "Editable styled equation"}, "assets": {}, "fonts": {},
                "equations": {"eq": {"latex": "x^2"}}, "figures": {}, "tables": {}, "charts": {},
                "protected": {"slideIds": [], "elementIds": ["equation"], "requiredText": []},
            }), encoding="utf-8")

            result = build_from_html(
                html_path=source, registry_path=registry_path,
                base_path=ROOT / "Bento_Slides.base.bento.html", output_path=output,
            )

            by_id = {item["id"]: item for item in result.document["slides"][0]["elements"]}
            self.assertEqual(by_id["equation"]["type"], "image")
            self.assertEqual(by_id["equation--label"]["equationId"], "eq")
            self.assertEqual(by_id["equation--label"]["latexSource"], "x^2")
            self.assertEqual(result.report["summary"]["bakedEquationCount"], 0)
            self.assertEqual(result.report["renderCalibration"]["equations"], 1)
            calibration_path = output.parent / "diagnostics/render-calibration.json"
            self.assertTrue(calibration_path.is_file())
            calibration = json.loads(calibration_path.read_text(encoding="utf-8"))
            measurement = calibration["measurements"][0]
            self.assertLess(
                measurement["initialBentoGlyphFrame"]["w"],
                by_id["equation--label"]["w"],
            )
            self.assertTrue(result.report["browserCheck"]["serialize_roundtrip"])

            with BrowserHarness() as harness:
                with harness.page("bentoCheck") as page:
                    page.goto(output.resolve().as_uri(), wait_until="load")
                    page.wait_for_selector('.ed-stage [data-el-id="equation--label"] math')
                    harness.settle(page)
                    overlay = page.locator('.ed-stage [data-el-id="equation--label"]')
                    self.assertEqual(overlay.locator("math").count(), 1)
                    self.assertNotIn("$", overlay.text_content() or "")

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

    def test_post_apply_review_rejects_rotated_bounds_and_shape_text_clipping(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "deck.preview.html"
            registry_path = root / "deck.registry.json"
            source.write_text(
                "<!doctype html><style>html,body{margin:0}.slide{position:relative;width:1280px;"
                "height:720px;overflow:hidden;background:#fff}.item{position:absolute}"
                ".rotated{left:1100px;top:100px;width:180px;height:180px;transform:rotate(45deg)}"
                ".clipped{left:80px;top:80px;width:100px;height:30px;overflow:hidden;white-space:nowrap}"
                "</style><main data-bento-deck>"
                "<section class='slide' data-slide-id='rotated' data-section-id='intro'>"
                "<div class='item rotated' data-bento-id='rotated-shape' data-bento-type='shape'></div>"
                "</section><section class='slide' data-slide-id='clipped' data-section-id='method'>"
                "<div class='item clipped' data-bento-id='clipped-shape' data-bento-type='shape'>"
                "This label is intentionally much wider than its shape</div></section></main>",
                encoding="utf-8",
            )
            registry_path.write_text(json.dumps({
                "format": "bento/html-registry/v2", "unitId": "deck", "sources": {},
                "document": {"title": "Strict post-apply review"},
                "assets": {}, "fonts": {}, "equations": {}, "figures": {},
                "tables": {}, "charts": {},
                "protected": {"slideIds": [], "elementIds": [], "requiredText": []},
            }), encoding="utf-8")

            with self.assertRaisesRegex(
                BrowserCheckError,
                r"rotated \(element-out-of-bounds\).*clipped \(element-content-overflow\)",
            ):
                collect_html_change_browser_evidence(
                    html_path=source,
                    registry_path=registry_path,
                    affected_slide_ids=["rotated", "clipped"],
                    screenshots_dir=root / "screenshots",
                )

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
