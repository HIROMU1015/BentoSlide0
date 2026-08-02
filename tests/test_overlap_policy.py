from __future__ import annotations

import copy
import unittest

from bento_converter.html_converter import _correct_overlaps, _overlap_pairs


def output(element_id, x, y, w=300, h=120):
    return {"id": element_id, "type": "text", "x": x, "y": y, "w": w, "h": h}


def source(element_id, x, y, *, role=None, group=None, element_type="text"):
    return {"id": element_id, "type": element_type, "x": x, "y": y, "layoutGroup": group, "role": role}


def run_policy(layout_name, outputs, sources):
    slides = [{"id": "s", "elements": copy.deepcopy(outputs)}]
    corrections, diagnostics = [], []
    _correct_overlaps(slides, ({"id": "s", "layout": layout_name, "elements": sources},), corrections, diagnostics)
    return slides[0]["elements"], corrections, diagnostics


class OverlapPolicyTests(unittest.TestCase):
    def test_two_column_preserves_sides(self) -> None:
        elements, corrections, _ = run_policy("two-column", [output("left", 100, 150, 600), output("right", 560, 150, 600)], [source("left", 100, 150), source("right", 700, 150)])
        self.assertLess(elements[0]["x"] + elements[0]["w"], 641)
        self.assertGreater(elements[1]["x"], 639)
        self.assertEqual(corrections[0]["policy"], "two-column-preserve-side")

    def test_observation_interpretation_keeps_left_right_order(self) -> None:
        elements, corrections, _ = run_policy("observation-interpretation", [output("observation", 100, 180, 600), output("interpretation", 550, 180, 600)], [source("observation", 100, 180, role="observation"), source("interpretation", 760, 180, role="interpretation")])
        self.assertLess(elements[0]["x"], elements[1]["x"])
        self.assertEqual(corrections[0]["policy"], "observation-arrow-interpretation-order")

    def test_stack_preserves_vertical_order(self) -> None:
        elements, corrections, _ = run_policy("stack", [output("top", 200, 100, 500, 260), output("bottom", 200, 300, 500, 260)], [source("top", 200, 100), source("bottom", 200, 400)])
        self.assertLess(elements[0]["y"] + elements[0]["h"], elements[1]["y"])
        self.assertEqual(corrections[0]["policy"], "stack-preserve-vertical-order")

    def test_row_uses_source_order_even_if_output_centers_cross(self) -> None:
        elements, corrections, _ = run_policy(
            "row",
            [output("first", 500, 120, 260), output("second", 420, 120, 260)],
            [source("first", 100, 120), source("second", 600, 120)],
        )
        first, second = elements
        self.assertLessEqual(first["x"] + first["w"] + 12, second["x"] + 0.01)
        self.assertEqual(corrections[0]["policy"], "row-preserve-horizontal-order")

    def test_equation_dissection_keeps_equation_above_explanation(self) -> None:
        elements, corrections, _ = run_policy(
            "equation-dissection",
            [output("equation", 200, 250, 700, 180), output("meaning", 220, 320, 650, 180)],
            [source("equation", 200, 120, element_type="equation"), source("meaning", 220, 420)],
        )
        equation, meaning = elements
        self.assertLessEqual(equation["y"] + equation["h"] + 12, meaning["y"] + 0.01)
        self.assertEqual(corrections[0]["policy"], "equation-above-explanations")

    def test_shared_layout_group_preserves_dominant_axis(self) -> None:
        elements, corrections, _ = run_policy(
            "grid",
            [output("a", 100, 200, 400), output("b", 350, 200, 400)],
            [source("a", 100, 200, group="g"), source("b", 700, 210, group="g")],
        )
        self.assertLess(elements[0]["x"], elements[1]["x"])
        self.assertEqual(corrections[0]["policy"], "layout-group-preserve-source-axis")

    def test_free_intentional_overlap_is_only_diagnosed(self) -> None:
        original = [output("back", 200, 200), output("front", 240, 220)]
        elements, corrections, diagnostics = run_policy("free", original, [source("back", 200, 200), source("front", 240, 220)])
        self.assertEqual(elements, original)
        self.assertFalse(corrections)
        self.assertEqual(diagnostics[0]["policy"], "diagnostic-intent-uncertain")

    def test_correction_rolls_back_if_it_creates_new_overlap(self) -> None:
        original = [output("a", 450, 100, 100), output("b", 500, 100, 100), output("c", 700, 100, 100)]
        elements, corrections, diagnostics = run_policy("two-column", original, [source("a", 450, 100), source("b", 500, 100), source("c", 700, 100)])
        self.assertEqual(elements, original)
        self.assertFalse(corrections)
        self.assertTrue(diagnostics)
        self.assertIn(("a", "b"), _overlap_pairs(elements))


if __name__ == "__main__":
    unittest.main()
