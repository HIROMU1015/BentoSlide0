from __future__ import annotations

import json
import shutil
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

from scripts.deck_workflow import WorkflowError, atomic_write_state, load_state
from scripts.run_html_preview import create_preview_server


ROOT = Path(__file__).resolve().parents[1]


class HtmlPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "workflow").mkdir()
        (self.root / "chapters/assets").mkdir(parents=True)
        shutil.copy2(ROOT / "deck.yaml", self.root / "deck.yaml")
        shutil.copy2(ROOT / "workflow/deck.schema.json", self.root / "workflow/deck.schema.json")
        shutil.copy2(ROOT / "workflow/deck.v1.schema.json", self.root / "workflow/deck.v1.schema.json")
        (self.root / "REQUEST.md").write_text("# Request\n", encoding="utf-8")
        (self.root / "chapters/chapter-01.preview.html").write_text(
            '<!doctype html><link rel="stylesheet" href="assets/theme.css"><section data-slide-id="slide-1">Preview</section>',
            encoding="utf-8",
        )
        (self.root / "chapters/assets/theme.css").write_text("body { color: #123456; }\n", encoding="utf-8")
        state = load_state(self.root)
        state["chapters"] = {
            "chapter-01": {
                "html": "chapters/chapter-01.preview.html",
                "registry": "chapters/chapter-01.registry.json",
                "status": "review",
                "visualApproval": "pending",
            }
        }
        state["workflow"].update(
            stage="html_review", status="awaiting_approval", owner="work",
            sourceOfTruth="chapters", currentChapter="chapter-01",
        )
        atomic_write_state(self.root, state)
        self.server = create_preview_server(self.root, port=self.free_port())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    @staticmethod
    def free_port() -> int:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return listener.getsockname()[1]

    def read(self, path: str) -> tuple[int, str, str]:
        with urlopen(self.base + path, timeout=3) as response:
            return response.status, response.headers.get_content_type(), response.read().decode("utf-8")

    def test_index_lists_and_highlights_current_chapter(self) -> None:
        status, content_type, body = self.read("/")
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "text/html")
        self.assertIn("chapter-01.preview.html", body)
        self.assertIn("current", body)
        self.assertIn("html_review", body)

    def test_status_is_dynamic_and_machine_readable(self) -> None:
        status, content_type, body = self.read("/api/status")
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "application/json")
        self.assertEqual(payload["format"], "bento/html-preview-status/v1")
        self.assertEqual(Path(payload["repository"]), self.root.resolve())
        self.assertEqual(payload["currentChapter"], "chapter-01")
        self.assertEqual(payload["currentPath"], "chapters/chapter-01.preview.html")
        self.assertEqual(payload["chapters"], ["chapters/chapter-01.preview.html"])

    def test_serves_chapter_html_css_and_head(self) -> None:
        self.assertIn("Preview", self.read("/chapters/chapter-01.preview.html")[2])
        status, content_type, body = self.read("/chapters/assets/theme.css")
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "text/css")
        self.assertIn("#123456", body)
        request = __import__("urllib.request", fromlist=["Request"]).Request(
            self.base + "/chapters/chapter-01.preview.html", method="HEAD"
        )
        with urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"")

    def test_traversal_and_non_chapter_files_are_rejected(self) -> None:
        for path in ("/%2e%2e/deck.yaml", "/chapters/%2e%2e/deck.yaml", "/deck.yaml", "/chapters\\..\\deck.yaml"):
            with self.subTest(path=path), self.assertRaises(HTTPError) as captured:
                urlopen(self.base + path, timeout=3)
            self.assertEqual(captured.exception.code, 404)

    def test_external_bind_and_port_conflict_are_rejected(self) -> None:
        with self.assertRaisesRegex(WorkflowError, "127.0.0.1"):
            create_preview_server(self.root, host="0.0.0.0", port=self.free_port())
        with self.assertRaises(OSError):
            other = create_preview_server(self.root, port=self.server.server_port)
            other.server_close()


if __name__ == "__main__":
    unittest.main()
