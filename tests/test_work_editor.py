from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from bento_converter.errors import BentoConverterError, ValidationError
from bento_converter.artifact_transaction import ArtifactLeaseConflict
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

            current_html = load_html(storage.target)
            invalid_document = extract_bento_doc(current_html)
            invalid_document["slides"] = []
            code, _ = self.post(base + "/api/save", {
                "baseRevision": saved["revision"],
                "serializedHtml": embed_bento_doc(current_html, invalid_document),
            })
            self.assertEqual(code, 422)

            protected_document = extract_bento_doc(current_html)
            next(
                element for slide in protected_document["slides"] for element in slide["elements"]
                if element["id"] == "slide-1-title"
            )["html"] = "Changed"
            code, _ = self.post(base + "/api/save", {
                "baseRevision": saved["revision"],
                "serializedHtml": embed_bento_doc(current_html, protected_document),
            })
            self.assertEqual(code, 422)

            code, checked = self.post(base + "/api/validate", {"baseRevision": saved["revision"], "serializedHtml": load_html(storage.target)})
            self.assertEqual((code, checked["validation"]), (200, "pass"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_finalization_root_waits_for_consistent_transaction_snapshot(self) -> None:
        storage = self.storage()
        edited, revision, expected_document = self.edited_html(storage, delta=9)
        replacement_started = threading.Event()
        allow_commit = threading.Event()
        save_errors: list[BaseException] = []
        get_errors: list[BaseException] = []
        response: list[str] = []

        def pause(event: str, _journal: dict) -> None:
            if event == "replaced:0":
                replacement_started.set()
                if not allow_commit.wait(timeout=10):
                    raise RuntimeError("Timed out waiting to complete final transaction")

        storage.transactions.fault_injector = pause
        server, thread, base = self.running_server(storage)

        def save() -> None:
            try:
                storage.save_serialized(edited, base_revision=revision)
            except BaseException as exc:
                save_errors.append(exc)

        def get_root() -> None:
            try:
                response.append(self.get(base + "/")[1])
            except BaseException as exc:
                get_errors.append(exc)

        save_thread = threading.Thread(target=save)
        get_thread = threading.Thread(target=get_root)
        try:
            save_thread.start()
            self.assertTrue(replacement_started.wait(timeout=10))
            get_thread.start()
            time.sleep(0.2)
            self.assertTrue(get_thread.is_alive(), "GET / returned during a partial final replacement")
            allow_commit.set()
            save_thread.join(timeout=10)
            get_thread.join(timeout=10)
            self.assertEqual(save_errors, [])
            self.assertEqual(get_errors, [])
            self.assertEqual(extract_bento_doc(response[0]), expected_document)
        finally:
            allow_commit.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_non_loopback_bind_is_rejected(self) -> None:
        with self.assertRaises(BentoConverterError):
            create_work_editor_server(self.storage(), host="0.0.0.0", port=0)

    def test_server_holds_writer_lease_until_close(self) -> None:
        first = self.storage()
        server = create_work_editor_server(first, port=0)
        try:
            self.assertTrue(first.writer_lease_acquired)
            with self.assertRaises(ArtifactLeaseConflict):
                self.storage()
        finally:
            server.server_close()
        self.assertFalse(first.writer_lease_acquired)
        second_server = create_work_editor_server(self.storage(), port=0)
        second_server.server_close()


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

        source_html = load_html(self.source)
        exception_shim = """<script id="serialize-exception-test-shim">
(() => {
  const timer = setInterval(() => {
    if (!window.bento || typeof window.bento.serialize !== 'function') return;
    const current = window.bento.serialize;
    const exceptionWrapper = (...args) => {
      if (window.__throwBentoSerializeForTest) throw new Error('serialize-test-error');
      return current.apply(window.bento, args);
    };
    exceptionWrapper.workEditorGuard = current.workEditorGuard === true;
    window.bento.serialize = exceptionWrapper;
    clearInterval(timer);
  }, 1);
})();
</script>
"""
        self.source.write_text(source_html.replace("</body>", exception_shim + "</body>"), encoding="utf-8")
        storage = self.storage()
        generated_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        runtime_before = runtime_fingerprint(load_html(storage.target))
        revision_before = storage.status()["revision"]
        immediate_status = storage.status

        def delayed_status() -> dict:
            time.sleep(0.5)
            return immediate_status()

        storage.status = delayed_status  # type: ignore[method-assign]
        server, thread, base = self.running_server(storage)
        try:
            with sync_playwright() as playwright:
                executable = find_browser_executable()
                browser = playwright.chromium.launch(headless=True, **({"executable_path": str(executable)} if executable else {}))
                page = browser.new_page(viewport={"width": 1400, "height": 900})

                page.goto(self.source.as_uri(), wait_until="load")
                page.wait_for_function("window.bento && typeof window.bento.serialize === 'function'")
                before_injection_contract = page.evaluate("""() => {
                  const result = window.bento.serialize();
                  return {
                    isString: typeof result === 'string',
                    isPromise: result instanceof Promise,
                    hasThen: Boolean(result && typeof result.then === 'function')
                  };
                }""")

                page.goto(base + "/", wait_until="load")
                page.wait_for_function("window.bento && typeof window.bento.serialize === 'function'")
                page.wait_for_function("document.querySelector('#work-save') && !document.querySelector('#work-save').disabled")
                serialize_contract = page.evaluate("""() => {
                  const host = document.getElementById('bento-work-editor-host');
                  const parent = host.parentNode;
                  const next = host.nextSibling;
                  const result = window.bento.serialize();
                  const toolbarIds = [
                    'bento-work-editor',
                    'bento-work-editor-host',
                    'bento-work-editor-loader',
                    'bento-work-editor-style'
                  ];
                  const buttons = ['work-save', 'work-save-validate', 'work-revert', 'work-reload'];
                  return {
                    isString: typeof result === 'string',
                    isPromise: result instanceof Promise,
                    hasThen: Boolean(result && typeof result.then === 'function'),
                    guardMarker: window.bento.serialize.workEditorGuard === true,
                    excludedToolbarIds: toolbarIds.every(id => !result.includes(id)),
                    containsWorkEditor: result.includes('bento-work-editor'),
                    sameParent: host.parentNode === parent,
                    sameNextSibling: host.nextSibling === next,
                    connected: host.isConnected,
                    visible: getComputedStyle(host).display !== 'none',
                    buttonsUsable: buttons.every(id => {
                      const button = document.getElementById(id);
                      return Boolean(button && button.isConnected && !button.disabled);
                    })
                  };
                }""")
                detached_contract = page.evaluate("""() => {
                  const host = document.getElementById('bento-work-editor-host');
                  const parent = host.parentNode;
                  const next = host.nextSibling;
                  host.remove();
                  try {
                    const result = window.bento.serialize();
                    return {
                      isString: typeof result === 'string',
                      isPromise: result instanceof Promise,
                      hasThen: Boolean(result && typeof result.then === 'function'),
                      remainedDetached: !host.isConnected
                    };
                  } finally {
                    parent.insertBefore(host, next);
                  }
                }""")
                exception_contract = page.evaluate("""() => {
                  const host = document.getElementById('bento-work-editor-host');
                  const parent = host.parentNode;
                  const next = host.nextSibling;
                  let error = null;
                  window.__throwBentoSerializeForTest = true;
                  try {
                    window.bento.serialize();
                  } catch (caught) {
                    error = caught.message;
                  } finally {
                    window.__throwBentoSerializeForTest = false;
                  }
                  return {
                    error,
                    sameParent: host.parentNode === parent,
                    sameNextSibling: host.nextSibling === next,
                    connected: host.isConnected,
                    visible: getComputedStyle(host).display !== 'none'
                  };
                }""")
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
                page.wait_for_function("document.querySelector('#work-save') && !document.querySelector('#work-save').disabled")

                page.locator("#work-save-validate").click()
                page.wait_for_function("document.querySelector('#bento-work-editor-status').textContent.includes('保存・検証しました')")
                with page.expect_navigation(wait_until="load"):
                    page.locator("#work-reload").click()
                page.wait_for_function("document.querySelector('#work-save') && !document.querySelector('#work-save').disabled")
                with page.expect_navigation(wait_until="load"):
                    page.locator("#work-revert").click()
                page.wait_for_function("document.querySelector('#work-save') && !document.querySelector('#work-save').disabled")

                after = page.evaluate("window.bento.doc.slides.flatMap(s=>s.elements).find(e=>e.type==='shape').x")
                serialized = page.evaluate("""() => {
                  const result = window.bento.serialize();
                  if (typeof result !== 'string') throw new Error('serialize result is not a string');
                  return result;
                }""")
                browser.close()

            self.assertEqual(before_injection_contract, {"isString": True, "isPromise": False, "hasThen": False})
            self.assertTrue(serialize_contract["isString"])
            self.assertFalse(serialize_contract["isPromise"])
            self.assertFalse(serialize_contract["hasThen"])
            self.assertTrue(serialize_contract["guardMarker"])
            self.assertTrue(serialize_contract["excludedToolbarIds"])
            self.assertFalse(serialize_contract["containsWorkEditor"])
            self.assertTrue(serialize_contract["sameParent"])
            self.assertTrue(serialize_contract["sameNextSibling"])
            self.assertTrue(serialize_contract["connected"])
            self.assertTrue(serialize_contract["visible"])
            self.assertTrue(serialize_contract["buttonsUsable"])
            self.assertEqual(detached_contract, {
                "isString": True, "isPromise": False, "hasThen": False, "remainedDetached": True,
            })
            self.assertEqual(exception_contract["error"], "serialize-test-error")
            self.assertTrue(exception_contract["sameParent"])
            self.assertTrue(exception_contract["sameNextSibling"])
            self.assertTrue(exception_contract["connected"])
            self.assertTrue(exception_contract["visible"])
            self.assertEqual(after, before + 2)
            for identifier in (
                "bento-work-editor", "bento-work-editor-host",
                "bento-work-editor-loader", "bento-work-editor-style",
            ):
                self.assertNotIn(identifier, serialized)
            self.assertEqual(extract_bento_doc(serialized), extract_bento_doc(load_html(storage.target)))
            self.assertEqual(json.loads(storage.sidecar.read_text(encoding="utf-8")), extract_bento_doc(serialized))
            self.assertNotEqual(storage.status()["revision"], revision_before)
            self.assertEqual(runtime_fingerprint(load_html(storage.target)), runtime_before)
            self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), generated_hash)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
