from __future__ import annotations

import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import yaml

from bento_converter.artifact_transaction import bytes_revision
from bento_converter.authoring_storage import (
    AUTHORING_BACKUP_FORMAT,
    AuthoringArtifactStorage,
    AuthoringConflict,
    validate_content_provenance,
)
from bento_converter.errors import BentoConverterError, ValidationError
from bento_converter.html_document import embed_bento_doc, extract_bento_doc, load_html, runtime_fingerprint
from bento_converter.work_editor import create_work_editor_server
from bento_converter.work_editor_client import discover_work_editor


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

    def test_no_op_save_does_not_create_backup_report_or_transaction(self) -> None:
        storage = self.storage()
        status = storage.status()
        report_before = storage.report_path.read_bytes() if storage.report_path.exists() else None
        result = storage.save_serialized(
            load_html(self.target),
            base_document_revision=status["documentRevision"],
            base_registry_revision=status["registryRevision"],
        )
        self.assertTrue(result["noOp"])
        self.assertIsNone(result["transactionId"])
        self.assertFalse(result["contentApprovalInvalidated"])
        self.assertEqual(storage._backups(), [])
        report_after = storage.report_path.read_bytes() if storage.report_path.exists() else None
        self.assertEqual(report_after, report_before)

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

    def test_revision_backup_transaction_writes_complete_manifest_and_reverts(self) -> None:
        storage = self.storage()
        original = extract_bento_doc(load_html(self.target))
        status = storage.status()
        html = load_html(self.target)
        document = extract_bento_doc(html)
        self.title(document)["html"] = "Manifest-backed edit"
        saved = storage.save_serialized(
            embed_bento_doc(html, document),
            base_document_revision=status["documentRevision"],
            base_registry_revision=status["registryRevision"],
        )
        backups = storage._backups()
        self.assertEqual(len(backups), 1)
        html_backup = backups[0]
        json_backup, registry_backup, manifest_path = storage._backup_paths(html_backup)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["format"], AUTHORING_BACKUP_FORMAT)
        self.assertEqual(manifest["files"]["html"]["revision"], bytes_revision(html_backup.read_bytes()))
        self.assertEqual(manifest["files"]["json"]["revision"], bytes_revision(json_backup.read_bytes()))
        self.assertEqual(manifest["files"]["registry"]["revision"], bytes_revision(registry_backup.read_bytes()))

        reverted = storage.revert(
            base_document_revision=saved["documentRevision"],
            base_registry_revision=saved["registryRevision"],
        )
        self.assertTrue(reverted["reverted"])
        self.assertEqual(extract_bento_doc(load_html(self.target)), original)
        self.assertFalse(any(path.exists() for path in (html_backup, json_backup, registry_backup, manifest_path)))

    def test_backup_crash_after_first_replace_recovers_without_exposing_candidate(self) -> None:
        storage = self.storage()

        def crash(event: str, _journal: dict) -> None:
            if event == "replaced:0":
                raise SimulatedCrash()

        storage.backup_fault_injector = crash
        with self.assertRaises(SimulatedCrash):
            storage.create_revision_backup()
        self.assertEqual(len(list(storage.revisions_dir.glob("*.bento.html"))), 1)
        self.assertEqual(storage._backups(), [])

        recovered = self.storage()
        self.assertEqual(recovered.status()["backupCount"], 0)
        self.assertEqual(list(recovered.revisions_dir.glob("*.rev-*")), [])

    def test_incomplete_backup_is_not_a_revert_candidate(self) -> None:
        storage = self.storage()
        storage.revisions_dir.mkdir(parents=True, exist_ok=True)
        incomplete = storage.revisions_dir / "presentation.authoring.rev-000001.bento.html"
        incomplete.write_bytes(self.target.read_bytes())
        self.assertEqual(storage.status()["backupCount"], 0)
        status = storage.status()
        with self.assertRaisesRegex(BentoConverterError, "No authoring revision backup"):
            storage.revert(
                base_document_revision=status["documentRevision"],
                base_registry_revision=status["registryRevision"],
            )

    def test_complete_legacy_backup_receives_manifest_before_use(self) -> None:
        storage = self.storage()
        storage.revisions_dir.mkdir(parents=True, exist_ok=True)
        html_backup = storage.revisions_dir / "presentation.authoring.rev-000001.bento.html"
        json_backup, registry_backup, manifest_path = storage._backup_paths(html_backup)
        html_backup.write_bytes(storage.target.read_bytes())
        json_backup.write_bytes(storage.sidecar.read_bytes())
        registry_backup.write_bytes(storage.registry_path.read_bytes())
        self.assertFalse(manifest_path.exists())

        migrated = self.storage()
        self.assertTrue(manifest_path.is_file())
        self.assertEqual(migrated.status()["backupCount"], 1)
        self.assertTrue(migrated._valid_backup(html_backup))

    def test_concurrent_authoring_saves_create_one_complete_backup_number(self) -> None:
        storage = self.storage()
        status = storage.status()
        html = load_html(self.target)
        first = extract_bento_doc(html)
        second = extract_bento_doc(html)
        self.title(first)["html"] = "First concurrent authoring edit"
        self.title(second)["html"] = "Second concurrent authoring edit"
        barrier = threading.Barrier(3)
        outcomes: list[str] = []

        def save(document: dict) -> None:
            barrier.wait()
            try:
                storage.save_serialized(
                    embed_bento_doc(html, document),
                    base_document_revision=status["documentRevision"],
                    base_registry_revision=status["registryRevision"],
                )
                outcomes.append("saved")
            except AuthoringConflict:
                outcomes.append("conflict")

        threads = [threading.Thread(target=save, args=(document,)) for document in (first, second)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(sorted(outcomes), ["conflict", "saved"])
        self.assertEqual(storage.status()["backupCount"], 1)
        self.assertEqual(
            [path.name for path in storage.revisions_dir.glob("*.manifest.json")],
            ["presentation.authoring.rev-000001.manifest.json"],
        )

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

    def test_content_review_rejects_every_unregistered_provenance_draft_kind(self) -> None:
        storage = self.storage()
        document = extract_bento_doc(load_html(self.target))
        equation = next(
            element for slide in document["slides"] for element in slide["elements"]
            if element["id"] == "hamiltonian-equation"
        )
        equation.pop("equationId")
        document["slides"][0]["elements"].extend([
            {"id": "draft-chart", "type": "chart", "x": 10, "y": 10, "w": 200, "h": 100, "preset": "bar", "option": {}},
            {
                "id": "draft-table", "type": "table", "x": 10, "y": 120, "w": 200, "h": 100,
                "columns": [{"w": 1}], "rows": [{"cells": [{"html": "1"}]}],
            },
            {
                "id": "source-image", "type": "image", "x": 10, "y": 230, "w": 200, "h": 100,
                "src": "data:image/png;base64,AAAA", "fit": "contain", "paperSource": "paper figure",
            },
            {
                "id": "draft-claim", "type": "shape", "x": 10, "y": 340, "w": 200, "h": 100,
                "shape": "rect", "unprovenancedDraft": True,
            },
        ])
        registry = json.loads(storage.registry_path.read_text(encoding="utf-8"))
        with self.assertRaises(ValidationError) as raised:
            validate_content_provenance(document, registry=registry)
        message = str(raised.exception)
        for field in ("equationId", "chartId", "tableId", "figureId/assetId", "unprovenancedDraft"):
            self.assertIn(field, message)

    def test_save_invalidates_approved_deck_state_in_the_same_transaction(self) -> None:
        initial = self.storage()
        status = initial.status()
        state_path = self.root / "deck.yaml"
        state_path.write_text(yaml.safe_dump({
            "schemaVersion": 2,
            "outputs": {
                "authoringHtml": "output/presentation.authoring.bento.html",
                "authoringJson": "output/presentation.authoring.bento.json",
                "authoringRegistry": "output/presentation.authoring.registry.json",
            },
            "approvals": {"bentoContent": {
                "status": "approved", "documentRevision": status["documentRevision"],
                "registryRevision": status["registryRevision"], "approvalDigest": "sha256:" + "1" * 64,
                "approvedAt": "2026-08-05T00:00:00Z",
            }},
        }, sort_keys=False), encoding="utf-8")
        storage = AuthoringArtifactStorage(
            source=self.generated, source_registry=self.generated_registry,
            target=self.target, target_registry=self.target_registry, repository=self.root,
            state_path=state_path,
        )
        self.assertEqual(storage.status()["contentApprovalStatus"], "approved")
        html = load_html(self.target)
        document = extract_bento_doc(html)
        self.title(document)["html"] = "Invalidate approved content"
        saved = storage.save_serialized(
            embed_bento_doc(html, document),
            base_document_revision=status["documentRevision"],
            base_registry_revision=status["registryRevision"],
        )
        self.assertTrue(saved["contentApprovalInvalidated"])
        updated = yaml.safe_load(state_path.read_text(encoding="utf-8"))
        self.assertEqual(updated["approvals"]["bentoContent"], {
            "status": "pending", "documentRevision": None, "registryRevision": None,
            "approvalDigest": None, "approvedAt": None,
        })
        journals = sorted((self.root / "output/.bento-transactions/archive").rglob("*.json"))
        authoring_save = next(
            json.loads(path.read_text(encoding="utf-8")) for path in journals
            if json.loads(path.read_text(encoding="utf-8"))["operation"] == "authoring-save"
        )
        targets = {Path(item["target"]).resolve() for item in authoring_save["artifacts"]}
        self.assertIn(state_path.resolve(), targets)

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
            session_path = self.root / "output/work-editor-session.json"
            session_path.write_text(json.dumps({
                "format": "bento/work-editor-session/v1", "host": "127.0.0.1",
                "port": server.server_address[1],
            }), encoding="utf-8")
            discovered = discover_work_editor(self.root, mode="authoring", target=self.target)
            self.assertEqual(discovered.status["editingMode"], "authoring")
            with self.assertRaisesRegex(BentoConverterError, "does not match"):
                discover_work_editor(self.root, mode="authoring", target=self.root / "output/other.bento.html")
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
                "replaceSlideIds": ["demo-slide-1"],
                "operation": "segment-replace",
                "operationReport": {"targetSlideId": "demo-slide-1"},
                "reportPath": "output/segment-reports/api-operation.json",
            }
            code, saved = post("/api/save", request)
            self.assertEqual(code, 200)
            self.assertTrue(saved["contentApprovalInvalidated"])
            self.assertIn("transactionId", saved)
            api_report = json.loads((self.root / "output/segment-reports/api-operation.json").read_text(encoding="utf-8"))
            self.assertEqual(api_report["operation"], "segment-replace")
            self.assertEqual(api_report["details"]["targetSlideId"], "demo-slide-1")
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

    def test_authoring_root_waits_for_consistent_transaction_snapshot(self) -> None:
        storage = self.storage()
        status = storage.status()
        html = load_html(self.target)
        document = extract_bento_doc(html)
        self.title(document)["html"] = "Consistent GET authoring edit"
        replacement_started = threading.Event()
        allow_commit = threading.Event()
        save_errors: list[BaseException] = []
        get_errors: list[BaseException] = []
        response: list[str] = []

        def pause(event: str, _journal: dict) -> None:
            if event == "replaced:0":
                replacement_started.set()
                if not allow_commit.wait(timeout=10):
                    raise RuntimeError("Timed out waiting to complete authoring transaction")
                raise RuntimeError("Force authoring rollback after the partial replacement")

        storage.transactions.fault_injector = pause
        server = create_work_editor_server(storage, port=0)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def save() -> None:
            try:
                storage.save_serialized(
                    embed_bento_doc(html, document),
                    base_document_revision=status["documentRevision"],
                    base_registry_revision=status["registryRevision"],
                )
            except BaseException as exc:
                save_errors.append(exc)

        def get_root() -> None:
            try:
                with urlopen(base + "/", timeout=10) as http_response:
                    response.append(http_response.read().decode("utf-8"))
            except BaseException as exc:
                get_errors.append(exc)

        save_thread = threading.Thread(target=save)
        get_thread = threading.Thread(target=get_root)
        try:
            save_thread.start()
            self.assertTrue(replacement_started.wait(timeout=10))
            get_thread.start()
            time.sleep(0.2)
            self.assertTrue(get_thread.is_alive(), "GET / returned during a partial artifact replacement")
            allow_commit.set()
            save_thread.join(timeout=10)
            get_thread.join(timeout=10)
            self.assertEqual(len(save_errors), 1)
            self.assertIn("Force authoring rollback", str(save_errors[0]))
            self.assertEqual(get_errors, [])
            self.assertEqual(self.title(extract_bento_doc(response[0]))["html"], "GPTが座標まで設計する")
        finally:
            allow_commit.set()
            server.shutdown(); server.server_close(); server_thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
