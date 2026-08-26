from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from PIL import Image

from bento_converter.errors import ValidationError
from bento_converter.html_layout import extract_computed_layout
from bento_converter.html_source import SourceChapter


@unittest.skipUnless(os.environ.get("BENTO_BROWSER_TEST") == "1", "Set BENTO_BROWSER_TEST=1 for Chromium capture tests.")
class FallbackCaptureScopeTests(unittest.TestCase):
    def chapter(self, root: Path, body: str) -> SourceChapter:
        html = root / "capture.preview.html"
        registry = root / "capture.registry.json"
        html.write_text(
            "<!doctype html><style>body{margin:0}.slide{position:relative;width:1280px;height:720px;overflow:hidden}"
            ".fallback{position:absolute;left:100px;top:100px;width:200px;height:100px;transform:skewX(12deg)}</style>" + body,
            encoding="utf-8",
        )
        registry.write_text(json.dumps({"format": "bento/html-registry/v1", "chapterId": "capture"}), encoding="utf-8")
        return SourceChapter("capture", html, registry, {})

    def test_same_element_id_on_different_slides_is_scoped_and_captured(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            chapter = self.chapter(
                root,
                '<section class="slide" data-slide-id="red"><div class="fallback" data-bento-id="shared" style="background:red"></div></section>'
                '<section class="slide" data-slide-id="blue"><div class="fallback" data-bento-id="shared" style="background:blue"></div></section>',
            )
            result = extract_computed_layout([chapter], root / "screenshots")
            self.assertEqual(set(result.image_fallbacks), {"red/shared", "blue/shared"})
            self.assertNotEqual(result.image_fallbacks["red/shared"], result.image_fallbacks["blue/shared"])

    def test_duplicate_id_inside_one_slide_reports_count_and_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            chapter = self.chapter(
                root,
                '<section class="slide" data-slide-id="duplicate">'
                '<div class="fallback" data-bento-id="same"></div><div class="fallback" data-bento-id="same"></div></section>',
            )
            with self.assertRaises(ValidationError) as caught:
                extract_computed_layout([chapter], root / "screenshots")
            message = str(caught.exception)
            self.assertIn("slideId='duplicate'", message)
            self.assertIn("elementId='same'", message)
            self.assertIn("'matchedElementCount': 2", message)
            self.assertIn("'captureReason': 'skew-transform'", message)
            self.assertIn("current slide", message)

    def test_background_capture_excludes_owned_and_annotated_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            chapter = self.chapter(
                root,
                '<section class="slide" data-slide-id="editable">'
                '<div class="fallback" data-bento-id="card" data-bento-type="shape" '
                'style="background:rgb(220,20,20);color:white;font:700 28px Arial">OWNED '
                '<span data-bento-id="label" data-bento-type="text" '
                'style="background:blue;color:white">CHILD</span></div></section>',
            )
            result = extract_computed_layout([chapter], root / "screenshots")
            slide = result.slides[0]
            card = next(item for item in slide["elements"] if item["id"] == "card")
            self.assertEqual(card["fallbackCapture"]["scope"], "background")
            self.assertEqual(card["fallbackCapture"]["excludedElementIds"], ["label"])
            self.assertTrue(card["fallbackCapture"]["ownedTextExcluded"])

            encoded = result.image_fallbacks["editable/card"].split(",", 1)[1]
            image = Image.open(BytesIO(base64.b64decode(encoded))).convert("RGBA")
            colors = [
                image.getpixel((x, y))
                for y in range(20, max(20, image.height - 20))
                for x in range(25, max(25, image.width - 25))
            ]
            self.assertFalse(any(
                alpha > 200 and red > 245 and green > 245 and blue > 245
                for red, green, blue, alpha in colors
            ))
            self.assertFalse(any(
                alpha > 200 and blue > 160 and red < 80
                for red, _green, blue, alpha in colors
            ))


if __name__ == "__main__":
    unittest.main()
