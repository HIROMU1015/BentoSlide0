from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bento_converter.html_converter import convert_html_layout
from bento_converter.html_layout import LayoutResult
from bento_converter.html_source import SourceChapter
from bento_converter.resource_embedding import ResourceResolutionError


def media_element(poster: str | None) -> dict[str, object]:
    return {
        "id": "demo-video", "type": "media", "exportMode": "auto", "domIndex": 0,
        "x": 10, "y": 10, "w": 320, "h": 180, "boundingFrame": {"x": 10, "y": 10, "w": 320, "h": 180},
        "rotation": 0, "opacity": 1, "z": 0, "text": "", "html": "", "outerHTML": "<video></video>",
        "src": "data:video/mp4;base64,AAAA", "poster": poster, "mediaKind": "video",
        "controls": True, "autoplay": False, "loop": False, "muted": False,
        "style": {"borderRadius": 0, "objectFit": "contain"},
    }


class MediaPosterTests(unittest.TestCase):
    def convert(self, root: Path, poster: str | None, *, assets: dict | None = None):
        html_path = root / "chapter.preview.html"
        registry_path = root / "chapter.registry.json"
        html_path.write_text("<!doctype html>", encoding="utf-8")
        registry_path.write_text("{}", encoding="utf-8")
        chapter = SourceChapter("chapter", html_path, registry_path, {"assets": assets or {}})
        registry = {
            "document": {"title": "Media", "modified": "2026-08-05T00:00:00Z"},
            "assets": assets or {}, "fonts": {}, "equations": {}, "figures": {}, "tables": {}, "charts": {},
            "protected": {"slideIds": [], "elementIds": [], "requiredText": []},
        }
        layout = LayoutResult(({
            "id": "slide", "background": "#fff", "transition": "none", "notes": "", "layout": "free",
            "elements": [media_element(poster)],
        },), (), {}, "test")
        return convert_html_layout(layout, registry, [chapter])

    def poster(self, result) -> str | None:
        return result.document["slides"][0]["elements"][0].get("poster")

    def test_relative_poster_is_embedded_and_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "poster.png").write_bytes(b"poster")
            result = self.convert(root, "poster.png")
            self.assertTrue(self.poster(result).startswith("data:image/png;base64,"))
            record = next(item for item in result.report["assetResolution"] if item.get("resourceField") == "poster")
            self.assertEqual(record["source"], "$SOURCE_ROOT/poster.png")

    def test_absolute_and_file_poster_are_embedded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            poster = root / "poster.png"
            poster.write_bytes(b"poster")
            for source in (str(poster.resolve()), poster.resolve().as_uri()):
                with self.subTest(source=source):
                    self.assertTrue(self.poster(self.convert(root, source)).startswith("data:image/png;base64,"))

    def test_asset_poster_is_embedded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self.convert(
                root, "asset:poster", assets={"poster": {"data": "data:image/png;base64,BBBB", "mimeType": "image/png"}},
            )
            self.assertEqual(self.poster(result), "data:image/png;base64,BBBB")

    def test_data_poster_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            value = "data:image/png;base64,CCCC"
            self.assertEqual(self.poster(self.convert(Path(temporary), value)), value)

    def test_missing_poster_field_is_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = self.convert(Path(temporary), None)
            self.assertIsNone(self.poster(result))

    def test_missing_poster_file_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ResourceResolutionError, "missing.png"):
                self.convert(Path(temporary), "missing.png")


if __name__ == "__main__":
    unittest.main()
