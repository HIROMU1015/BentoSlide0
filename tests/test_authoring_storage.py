from __future__ import annotations

import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from bento_converter.authoring_storage import AuthoringArtifactStorage, AuthoringConflict
from bento_converter.errors import ValidationError
from bento_converter.html_document import embed_bento_doc, extract_bento_doc, load_html, runtime_fingerprint
from bento_converter.work_editor import create_work_editor_server


ROOT = Path(__file__).resolve().parents[1]


class SimulatedCrash(BaseException):
    pass


class AuthoringArtifactStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        (self.root / "output/diagnostics").mkdir(parents=True)
        self.generated = self.root / "output/presentation.generated.bento.html"
        self.generated.write_bytes((ROOT / "demo.bento.html").read_bytes())
        self.generated_registry = self.root / "output/diagnostics/merged-registry.json"
        self.generated_registry.write_text(json.dumps({
            "format": "bento/html-registry/v2", "unitId": "deck", "sources": {},
            "document": {}, "assets": {}, "fonts": {},
            "equations": {"hamiltonian_split": {"latex": "H = H_0 + \\alpha H_1"}},
            "figures": {}, "tables": {}, "charts": {},
            "protected": {"slideIds": [], "elementIds": [], "requiredText": []},
        }), encoding="utf-8")
        self.target = self.root / "output/presentation.authoring.bento.html"
        self.target_registry = self.root / "output/presentation.authoring.registry.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def storage(self) -> AuthoringArtifactStorage:
        return AuthoringArtifactStorage(
            source=self.generated, source_registry=self.generated_registry,
            target=self.target, target_registry=self.target_registry, repository=self.root,
        )

    @staticmethod
    def title(document: dict) -> dict:
        return next(element for slide in document["slides"] for element in slide["elements"] if element["id"] == "slide-1-title")

    def test_initialization_and_text_save_commit_html_json_registry(self) -> None:
        storage = self.storage()
        before_runtime = runtime_fingerprint(load_html(self.target))
        status = storage.status()
        html = load_html(self.target)
        document = extract_bento_doc(html)
        self.title(document)["html"] = "Authoring text"
        result = storage.save_serialized(
            embed_bento_doc(html, document),
            base_document_revision=status["documentRevision"],
            base_registry_revision=status["registryRevision"],
        )
        self.assertEqual(extract_bento_doc(load_html(self.target)), document)
        self.assertEqual(json.loads(storage.sidecar.read_text(encoding="utf-8")), document)
        self.assertEqual(runtime_fingerprint(load_html(self.target)), before_runtime)
        self.assertEqual(result["registryRevision"], status["registryRevision"])
        self.assertTrue(result["contentApprovalInvalidated"])
        report = json.loads(storage.report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["transactionId"], result["transactionId"])
        self.assertIn("demo-slide-1", report["slides"]["changed"])

    def test_document_and_registry_base_revisions_are_both_required(self) -> None:
        storage = self.storage()
        status = storage.status()
        with self.assertRaises(AuthoringConflict):
            storage.save_serialized(
                load_html(self.target), base_document_revision="sha256:" + "0" * 64,
                base_registry_revision=status["registryRevision"],
            )
        with self.assertRaises(AuthoringConflict):
            storage.save_serialized(
                load_html(self.target), base_document_revision=status["documentRevision"],
                base_registry_revision="sha256:" + "0" * 64,
            )

    def test_registry_sensitive_change_requires_changed_registry_same_transaction(self) -> None:
        storage = self.storage()
        status = storage.status()
        html = load_html(self.target)
        document = extract_bento_doc(html)
        equation = next(element for slide in document["slides"] for element in slide["elements"] if element["id"] == "hamiltonian-equation")
        equation["latexSource"] = "H = H_0 + 2H_1"
        with self.assertRaisesRegex(ValidationError, "requires a registry update"):
            storage.save_serialized(
                embed_bento_doc(html, document),
                base_document_revision=status["documentRevision"], base_registry_revision=status["registryRevision"],
            )
        registry = json.loads(self.target_registry.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValidationError, "changed registry revision"):
            storage.save_serialized(
                embed_bento_doc(html, document), registry=copy.deepcopy(registry),
                base_document_revision=status["documentRevision"], base_registry_revision=status["registryRevision"],
            )
        registry["equations"]["hamiltonian_split"]["latex"] = "H = H_0 + 2H_1"
        saved = storage.save_serialized(
            embed_bento_doc(html, document), registry=registry,
            base_document_revision=status["documentRevision"], base_registry_revision=status["registryRevision"],
        )
        self.assertNotEqual(saved["registryRevision"], status["registryRevision"])

    def test_partial_crash_recovers_all_three_authoring_artifacts(self) -> None:
        storage = self.storage()
        before = (self.target.read_bytes(), storage.sidecar.read_bytes(), self.target_registry.read_bytes())
        status = storage.status()
        html = load_html(self.target)
        document = extract_bento_doc(html)
        self.title(document)["html"] = "Crash edit"

        def crash(event: str, _journal: dict) -> None:
            if event == "replaced:0":
                raise SimulatedCrash()

        storage.transactions.fault_injector = crash
        with self.assertRaises(SimulatedCrash):
            storage.save_serialized(
                embed_bento_doc(html, document),
                base_document_revision=status["documentRevision"], base_registry_revision=status["registryRevision"],
            )
        recovered = self.storage()
        self.assertEqual((self.target.read_bytes(), recovered.sidecar.read_bytes(), self.target_registry.read_bytes()), before)

    def test_slide_and_element_reordering_are_valid_authoring_edits(self) -> None:
        storage = self.storage()
        status = storage.status()
        html = load_html(self.target)
        document = extract_bento_doc(html)
        document["slides"].reverse()
        document["slides"][0]["elements"].reverse()
        saved = storage.save_serialized(
            embed_bento_doc(html, document),
            base_document_revision=status["documentRevision"], base_registry_revision=status["registryRevision"],
        )
        self.assertEqual(saved["validation"], "pass")
        self.assertEqual(extract_bento_doc(load_html(self.target))["slides"], document["slides"])

    def test_authoring_http_api_uses_dual_revisions_and_mode_contract(self) -> None:
        storage = self.storage()
        server = create_work_editor_server(storage, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def get(path: str):
            with urlopen(base + path, timeout=10) as response:
                return response.status, response.read().decode("utf-8")

        def post(path: str, payload: dict):
            request = Request(base + path, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urlopen(request, timeout=10) as response:
                    return response.status, json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                return exc.code, json.loads(exc.read().decode("utf-8"))

        try:
            code, raw = get("/api/status")
            status = json.loads(raw)
            self.assertEqual((code, status["editingMode"]), (200, "authoring"))
            self.assertEqual(Path(status["repository"]), self.root)
            self.assertEqual(status["sourceOfTruth"], "output/presentation.authoring.bento.html")
            html = load_html(self.target)
            document = extract_bento_doc(html)
            self.title(document)["html"] = "API edit"
            request = {
                "baseDocumentRevision": status["documentRevision"],
                "baseRegistryRevision": status["registryRevision"],
                "serializedHtml": embed_bento_doc(html, document),
            }
            code, saved = post("/api/save", request)
            self.assertEqual(code, 200)
            self.assertTrue(saved["contentApprovalInvalidated"])
            self.assertIn("transactionId", saved)
            code, _ = post("/api/save", request)
            self.assertEqual(code, 409)
            code, reverted = post("/api/revert", {
                "baseDocumentRevision": saved["documentRevision"],
                "baseRegistryRevision": saved["registryRevision"],
            })
            self.assertEqual((code, reverted["reverted"]), (200, True))
            self.assertIn("bento-work-editor-mode", get("/")[1])
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
