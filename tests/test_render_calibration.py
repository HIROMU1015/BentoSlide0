from __future__ import annotations

import unittest

from bento_converter.render_calibration import _apply_inline_math_fit, _inline_math_targets


def text(element_id: str, html: str, **extra) -> dict:
    return {"id": element_id, "type": "text", "html": html, "fontSize": 19, "x": 0, "y": 0, "w": 100, "h": 40, **extra}


class InlineMathTextFitTests(unittest.TestCase):
    def document(self) -> dict:
        return {"slides": [{"id": "slide-a", "elements": [
            text("lead", "動機：$S_{d}(Δt)$ の長時間極限を調べる"),
            text("plain", "数式なし"),
            text("equation", "$$x^2$$"),
            {"id": "panel", "type": "shape"},
            text("price", "US$5 and US$6"),
        ]}]}

    def test_only_text_with_inline_math_is_a_target(self) -> None:
        targets = _inline_math_targets(self.document())

        self.assertEqual([target["elementId"] for target in targets], ["lead", "price"])

    def test_scaled_text_records_a_bounded_font_correction(self) -> None:
        document = self.document()
        targets = [target for target in _inline_math_targets(document) if target["elementId"] == "lead"]

        corrections, measurements = _apply_inline_math_fit(
            targets, {("slide-a", "lead"): {"box": 40, "rendered": 59, "lineHeight": 29.45, "scale": 0.947}},
        )

        lead = document["slides"][0]["elements"][0]
        self.assertEqual(lead["fontSize"], 17.63)
        self.assertEqual(measurements[0]["status"], "scaled")
        self.assertEqual(corrections[0]["kind"], "inline-math-text-fit")
        self.assertEqual(corrections[0]["before"], {"fontSize": 19})
        self.assertFalse(corrections[0]["contentChanged"])

    def test_fitting_or_unresolved_text_is_left_unchanged(self) -> None:
        document = self.document()
        targets = _inline_math_targets(document)

        corrections, measurements = _apply_inline_math_fit(targets, {
            ("slide-a", "lead"): {"box": 40, "rendered": 36, "lineHeight": 29.45, "scale": 1},
            ("slide-a", "price"): {"box": 40, "rendered": 90, "lineHeight": 29.45, "scale": None},
        })

        self.assertEqual(corrections, [])
        self.assertEqual([item["status"] for item in measurements], ["fits", "unresolved"])
        self.assertEqual(document["slides"][0]["elements"][0]["fontSize"], 19)


if __name__ == "__main__":
    unittest.main()
