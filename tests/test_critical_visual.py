from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from bento_converter.html_converter import HtmlConversionResult
from bento_converter.html_layout import LayoutResult
from bento_converter.html_pipeline import _semantic_comparison
from bento_converter.visual_comparison import compare_crops


PASS = {"status": "pass", "normalizedPixelDifference": 0, "warnings": []}
FAIL = {"status": "fail", "normalizedPixelDifference": 1, "warnings": ["fixture failure"]}


def frame(size: float = 100) -> dict[str, float]:
    return {"x": 20, "y": 20, "w": size, "h": size}


class CriticalVisualTests(unittest.TestCase):
    def comparison(self, decision: dict, crop_result: dict) -> dict:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.png"
            bento = root / "bento.png"
            Image.new("RGB", (1280, 720), "white").save(source)
            Image.new("RGB", (1280, 720), "white").save(bento)
            base = {
                "slideId": "slide-1", "elementId": "element", "strategy": "native",
                "sourceFrame": frame(), "sourceBoundingFrame": frame(), "bentoFrame": frame(),
                "emittedIds": ["element"], "contentPreserved": True, "styleChecks": {},
            }
            base.update(decision)
            layout = LayoutResult(({"id": "slide-1"},), (str(source),), {}, "test")
            conversion = HtmlConversionResult(
                {"slides": [{"id": "slide-1", "elements": [{"id": "element", **frame()}]}]},
                {"elements": [base]},
            )
            with patch("bento_converter.html_pipeline.compare_images", return_value=PASS), patch(
                "bento_converter.html_pipeline.compare_crops", return_value=crop_result
            ):
                return _semantic_comparison(layout, conversion, (str(bento),), root)

    def test_equation_crop_failure_fails_slide(self) -> None:
        result = self.comparison({"sourceType": "equation"}, FAIL)
        self.assertFalse(result["passed"])
        item = result["pairs"][0]["elementComparisons"][0]
        self.assertEqual(item["statusContribution"], "slide-fail")
        self.assertEqual(item["criticalReason"], "sourceType=equation")

    def test_chart_crop_failure_fails_slide(self) -> None:
        result = self.comparison({"sourceType": "chart"}, FAIL)
        self.assertEqual(result["pairs"][0]["status"], "fail")

    def test_heading_crop_failure_fails_slide(self) -> None:
        result = self.comparison({"sourceType": "text", "sourceTag": "h1"}, FAIL)
        self.assertEqual(result["pairs"][0]["status"], "fail")

    def test_explicit_critical_text_crop_failure_fails_slide(self) -> None:
        result = self.comparison({"sourceType": "text", "critical": True}, FAIL)
        self.assertEqual(result["pairs"][0]["status"], "fail")
        self.assertEqual(
            result["pairs"][0]["elementComparisons"][0]["criticalReason"],
            "data-bento-critical=true",
        )

    def test_noncritical_requested_crop_failure_is_warning(self) -> None:
        result = self.comparison({"sourceType": "text", "compareCrop": True}, FAIL)
        self.assertTrue(result["passed"])
        item = result["pairs"][0]["elementComparisons"][0]
        self.assertFalse(item["critical"])
        self.assertEqual(item["statusContribution"], "slide-warning")
        self.assertEqual(result["pairs"][0]["status"], "warning")

    def test_tiny_identical_crop_is_comparable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "image.png"
            Image.new("RGB", (1280, 720), "white").save(image)
            result = compare_crops(image, image, frame(1), frame(1))
            self.assertIsNotNone(result)
            self.assertEqual(result["status"], "pass")


if __name__ == "__main__":
    unittest.main()
