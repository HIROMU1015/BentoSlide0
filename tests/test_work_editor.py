from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from bento_converter.errors import BentoConverterError, ValidationError
from bento_converter.browser_check import find_browser_executable
from bento_converter.html_document import embed_bento_doc, extract_bento_doc, load_html, runtime_fingerprint
from bento_converter.work_editor import create_work_editor_server
from bento_converter.work_editor_storage import WorkEditorConflict, WorkEditorStorage


ROOT = Path(__file__).resolve().parents[1]


class WorkEditorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "presentation.generated.bento.html"
        self.source.write_bytes((ROOT / "demo.bento.html").read_bytes())
        self.target = self.root / "presentation.final.bento.html"
        self.registry = self.root / "merged-registry.json"
        self.registry.write_text(json.dumps({
            "protected": {
                "slideIds": ["demo-slide-1"],
                "elementIds": ["slide-1-title"],
                "requiredText": ["GPTが座標まで設計する"],
            },
            "equations": {"hamiltonian_split": {"latex": "ignored by editor reference validation"}},
            "figures": {}, "charts": {}, "tables": {},
        }, ensure_ascii=False), encoding="utf-8")
        self.source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def storage(self, **updates) -> WorkEditorStorage:
        options = {"source": self.source, "target": self.target, "registry": self.registry}
        options.update(updates)
        return WorkEditorStorage(**options)

    @staticmethod
    def movable(document: dict) -> dict:
        return next(
            element for slide in document["slides"] for element in slide["elements"]
            if element["type"] == "shape"
        )

    def edited_html(self, storage: WorkEditorStorage, *, delta: int = 1) -> tuple[str, str, dict]:
        html = load_html(storage.target)
        document = extract_bento_doc(html)
        self.movable(document)["x"] += delta
        return embed_bento_doc(html, document), storage.status()["revision"], document

    def test_first_start_copies_generated_to_final_and_syncs_sidecar(self) -> None:
        storage = self.storage()
        self.assertEqual(storage.target.read_bytes(), self.source.read_bytes())
        self.assertEqual(extract_bento_doc(load_html(storage.target)), json.loads(storage.sidecar.read_text(encoding="utf-8")))
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.source_hash)

    def test_existing_final_is_not_overwritten_without_reset(self) -> None:
        storage = self.storage()
        edited, revision, document = self.edited_html(storage, delta=7)
        storage.save_serialized(edited, base_revision=revision)
        second = self.storage()
        self.assertEqual(self.movable(second.document_response()["document"])["x"], self.movable(document)["x"])
        reset = self.storage(reset_final=True)
        self.assertNotEqual(self.movable(reset.document_response()["document"])["x"], self.movable(document)["x"])

    def test_save_updates_only_bento_doc_syncs_json_and_creates_backup(self) -> None:
        storage = self.storage()
        before_html = load_html(storage.target)
        edited, revision, document = self.edited_html(storage)
        result = storage.save_serialized(edited, base_revision=revision)
        after_html = load_html(storage.target)
        self.assertTrue(result["saved"])
        self.assertEqual(runtime_fingerprint(before_html), runtime_fingerprint(after_html))
        self.assertEqual(extract_bento_doc(after_html), document)
        self.assertEqual(json.loads(storage.sidecar.read_text(encoding="utf-8")), document)
        self.assertEqual(result["backupCount"], 1)
        self.assertTrue(storage.save_report_path.is_file())
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.source_hash)

    def test_invalid_json_and_schema_failure_leave_target_unchanged(self) -> None:
        storage = self.storage()
        before = storage.target.read_bytes()
        with self.assertRaises(BentoConverterError):
            storage.save_serialized('<script id="bento-doc" type="application/bento+json">{bad}</script>', base_revision=storage.status()["revision"])
        self.assertEqual(storage.target.read_bytes(), before)
        html = load_html(storage.target)
        document = extract_bento_doc(html)
        document["slides"] = []
        with self.assertRaises(BentoConverterError):
            storage.save_serialized(embed_bento_doc(html, document), base_revision=storage.status()["revision"])
        self.assertEqual(storage.target.read_bytes(), before)

    def test_protected_content_change_is_rejected_unless_explicitly_allowed(self) -> None:
        storage = self.storage()
        html = load_html(storage.target)
        document = extract_bento_doc(html)
        title = next(element for slide in document["slides"] for element in slide["elements"] if element["id"] == "slide-1-title")
        title["html"] = "Changed"
        with self.assertRaises(ValidationError):
            storage.save_serialized(embed_bento_doc(html, document), base_revision=storage.status()["revision"])
        permissive_target = self.root / "permissive.final.bento.html"
        permissive = WorkEditorStorage(
            source=self.source, target=permissive_target, registry=None, allow_content_edit=True,
        )
        permissive_html = load_html(permissive.target)
        permissive_doc = extract_bento_doc(permissive_html)
        next(element for slide in permissive_doc["slides"] for element in slide["elements"] if element["id"] == "slide-1-title")["html"] = "Changed"
        result = permissive.save_serialized(
            embed_bento_doc(permissive_html, permissive_doc), base_revision=permissive.status()["revision"],
        )
        self.assertTrue(result["saved"])

    def test_new_content_requires_explicit_content_edit_option(self) -> None:
        storage = self.storage()
        html = load_html(storage.target)
        document = extract_bento_doc(html)
        added = dict(self.movable(document))
        added["id"] = "new-final-content"
        document["slides"][0]["elements"].append(added)
        with self.assertRaises(ValidationError):
            storage.save_serialized(embed_bento_doc(html, document), base_revision=storage.status()["revision"])

    def test_stale_revision_is_rejected(self) -> None:
        storage = self.storage()
        edited, revision, _ = self.edited_html(storage)
        storage.save_serialized(edited, base_revision=revision)
        with self.assertRaises(WorkEditorConflict):
            storage.save_serialized(edited, base_revision=revision)

    def test_concurrent_saves_allow_exactly_one_revision(self) -> None:
        storage = self.storage()
        html = load_html(storage.target)
        revision = storage.status()["revision"]
        first = extract_bento_doc(html)
        second = extract_bento_doc(html)
        self.movable(first)["x"] += 1
        self.movable(second)["x"] += 2
        barrier = threading.Barrier(3)
        outcomes: list[str] = []

        def save(document: dict) -> None:
            barrier.wait()
            try:
                storage.save_serialized(embed_bento_doc(html, document), base_revision=revision)
                outcomes.append("saved")
            except WorkEditorConflict:
                outcomes.append("conflict")

        threads = [threading.Thread(target=save, args=(document,)) for document in (first, second)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(sorted(outcomes), ["conflict", "saved"])

    def test_revert_restores_previous_html_and_json(self) -> None:
        storage = self.storage()
        original = extract_bento_doc(load_html(storage.target))
        edited, revision, _ = self.edited_html(storage)
        saved = storage.save_serialized(edited, base_revision=revision)
        reverted = storage.revert(base_revision=saved["revision"])
        self.assertTrue(reverted["reverted"])
        self.assertEqual(extract_bento_doc(load_html(storage.target)), original)
        self.assertEqual(json.loads(storage.sidecar.read_text(encoding="utf-8")), original)

    def test_backup_retention_limit(self) -> None:
        storage = self.storage(backup_limit=2)
        for _ in range(3):
            edited, revision, _ = self.edited_html(storage)
            storage.save_serialized(edited, base_revision=revision)
        self.assertEqual(storage.status()["backupCount"], 2)

    def running_server(self, storage: WorkEditorStorage):
        server = create_work_editor_server(storage, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread, f"http://127.0.0.1:{server.server_address[1]}"

    @staticmethod
    def get(url: str) -> tuple[int, str]:
        with urlopen(url, timeout=10) as response:
            return response.status, response.read().decode("utf-8")

    @staticmethod
    def post(url: str, payload: dict) -> tuple[int, dict]:
        request = Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_http_endpoints_toolbar_and_conflict(self) -> None:
        storage = self.storage()
        server, thread, base = self.running_server(storage)
        try:
            status, page = self.get(base + "/")
            self.assertEqual(status, 200)
            self.assertIn("bento-work-editor", page)
            self.assertNotIn("bento-work-editor", load_html(storage.target))
            _, status_payload = self.get(base + "/api/status")
            _, document_payload = self.get(base + "/api/document")
            status_json = json.loads(status_payload)
            document_json = json.loads(document_payload)
            self.assertEqual(status_json["revision"], document_json["revision"])
            html, revision, _ = self.edited_html(storage)
            code, saved = self.post(base + "/api/save", {"baseRevision": revision, "serializedHtml": html})
            self.assertEqual(code, 200)
            code, conflict = self.post(base + "/api/save", {"baseRevision": revision, "serializedHtml": html})
            self.assertEqual(code, 409)
            self.assertIn("already", conflict["error"])
            code, checked = self.post(base + "/api/validate", {"baseRevision": saved["revision"], "serializedHtml": load_html(storage.target)})
            self.assertEqual((code, checked["validation"]), (200, "pass"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_non_loopback_bind_is_rejected(self) -> None:
        with self.assertRaises(BentoConverterError):
            create_work_editor_server(self.storage(), host="0.0.0.0", port=0)


@unittest.skipUnless(os.environ.get("BENTO_BROWSER_TEST") == "1", "Set BENTO_BROWSER_TEST=1 for Work editor browser round-trip.")
class WorkEditorBrowserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "presentation.generated.bento.html"
        self.source.write_bytes((ROOT / "demo.bento.html").read_bytes())
        self.target = self.root / "presentation.final.bento.html"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def storage(self) -> WorkEditorStorage:
        return WorkEditorStorage(source=self.source, target=self.target)

    @staticmethod
    def running_server(storage: WorkEditorStorage):
        server = create_work_editor_server(storage, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread, f"http://127.0.0.1:{server.server_address[1]}"

    def test_browser_serialize_save_reload_roundtrip(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover
            self.skipTest(str(exc))
        storage = self.storage()
        server, thread, base = self.running_server(storage)
        try:
            with sync_playwright() as playwright:
                executable = find_browser_executable()
                browser = playwright.chromium.launch(headless=True, **({"executable_path": str(executable)} if executable else {}))
                page = browser.new_page(viewport={"width": 1400, "height": 900})
                page.goto(base + "/", wait_until="load")
                page.wait_for_function("window.bento && typeof window.bento.serialize === 'function'")
                before = page.evaluate("window.bento.doc.slides.flatMap(s=>s.elements).find(e=>e.type==='shape').x")
                loaded = page.evaluate("""() => {
                  const doc = JSON.parse(JSON.stringify(window.bento.doc));
                  doc.slides.flatMap(s=>s.elements).find(e=>e.type==='shape').x += 2;
                  return window.bento.loadDoc(JSON.stringify(doc));
                }""")
                self.assertTrue(loaded)
                page.locator("#work-save").click()
                page.wait_for_function("document.querySelector('#bento-work-editor-status').textContent.includes('保存しました')")
                page.reload(wait_until="load")
                page.wait_for_function("window.bento && typeof window.bento.serialize === 'function'")
                after = page.evaluate("window.bento.doc.slides.flatMap(s=>s.elements).find(e=>e.type==='shape').x")
                serialized = page.evaluate("window.bento.serialize()")
                browser.close()
            self.assertEqual(after, before + 2)
            self.assertNotIn("bento-work-editor", serialized)
            self.assertEqual(extract_bento_doc(serialized), extract_bento_doc(load_html(storage.target)))
            self.assertEqual(json.loads(storage.sidecar.read_text(encoding="utf-8")), extract_bento_doc(serialized))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
