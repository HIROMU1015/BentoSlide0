from __future__ import annotations

import unittest

from PIL import Image, ImageDraw

from bento_converter.visual_comparison import compare_images


def canvas(background: str = "white") -> Image.Image:
    return Image.new("RGB", (1280, 720), background)


class VisualComparisonTests(unittest.TestCase):
    def test_identical_images_pass(self) -> None:
        source = canvas()
        ImageDraw.Draw(source).rectangle((120, 120, 520, 420), fill="#2563eb")
        result = compare_images(source, source)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["normalizedPixelDifference"], 0)

    def test_minor_font_like_difference_is_pass_or_warning(self) -> None:
        source, bento = canvas(), canvas()
        ImageDraw.Draw(source).text((100, 100), "Research title", fill="black", stroke_width=1)
        ImageDraw.Draw(bento).text((102, 100), "Research title", fill="black")
        self.assertIn(compare_images(source, bento)["status"], {"pass", "warning"})

    def test_missing_background_fails(self) -> None:
        self.assertEqual(compare_images(canvas("#1e3a8a"), canvas("white"))["status"], "fail")

    def test_missing_primary_element_fails(self) -> None:
        source, bento = canvas(), canvas()
        ImageDraw.Draw(source).rectangle((180, 120, 1100, 600), fill="#2563eb")
        self.assertEqual(compare_images(source, bento)["status"], "fail")

    def test_large_position_shift_fails(self) -> None:
        source, bento = canvas(), canvas()
        ImageDraw.Draw(source).rectangle((80, 160, 540, 600), fill="#7c3aed")
        ImageDraw.Draw(bento).rectangle((740, 160, 1200, 600), fill="#7c3aed")
        self.assertEqual(compare_images(source, bento)["status"], "fail")


if __name__ == "__main__":
    unittest.main()
