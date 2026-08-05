from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from bento_converter.html_document import extract_bento_doc, load_html, serialize_bento_doc
from bento_converter.registry_document import REGISTRY_V2, registry_revision
from bento_converter.work_editor_storage import document_revision, protected_content_fingerprint
from scripts.deck_workflow import (
    WorkflowError,
    command_migrate,
    load_state,
    migrate_v1_state,
    validate_state,
)


ROOT = Path(__file__).resolve().parents[1]


class DeckMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "workflow").mkdir()
        (self.root / "sources/private").mkdir(parents=True)
        (self.root / "output").mkdir()
        for relative in ("deck.yaml", "workflow/deck.schema.json", "workflow/deck.v1.schema.json"):
            shutil.copy2(ROOT / relative, self.root / relative)
        (self.root / "REQUEST.md").write_text("# Request\n", encoding="utf-8")
        (self.root / "sources/private/paper.pdf").write_bytes(b"%PDF-1.4\n")
        state = yaml.safe_load((self.root / "deck.yaml").read_text(encoding="utf-8"))
        state["project"]["primarySource"] = "sources/private/paper.pdf"
        (self.root / "deck.yaml").write_text(
            yaml.safe_dump(state, allow_unicode=True, sort_keys=False), encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def raw_state(self) -> dict:
        return yaml.safe_load((self.root / "deck.yaml").read_text(encoding="utf-8"))

    def prepare_late_stage(self, *, registry: bool = True) -> tuple[bytes, dict]:
        state = self.raw_state()
        state["workflow"].update(
            stage="bento_finalization", status="in_progress", owner="work",
            sourceOfTruth="final", currentChapter=None,
        )
        state["handoff"].update(readyForCodex=False, readyForFinalEditing=True)
        html = load_html(ROOT / "demo.bento.html")
        document = extract_bento_doc(html)
        output = self.root / "output"
        (output / "presentation.generated.bento.html").write_text(html, encoding="utf-8")
        (output / "presentation.generated.bento.json").write_text(
            serialize_bento_doc(document) + "\n", encoding="utf-8",
        )
        (output / "presentation.final.bento.html").write_text(html, encoding="utf-8")
        (output / "presentation.final.bento.json").write_text(
            serialize_bento_doc(document) + "\n", encoding="utf-8",
        )
        revisions = output / "revisions"
        revisions.mkdir()
        baseline = revisions / "presentation.final.baseline.bento.json"
        baseline.write_text(serialize_bento_doc(document) + "\n", encoding="utf-8")
        state["validation"]["finalBaseline"] = {
            "path": "output/revisions/presentation.final.baseline.bento.json",
            "documentRevision": document_revision(document),
            "protectedContentFingerprint": protected_content_fingerprint(document),
        }
        if registry:
            diagnostics = output / "diagnostics"
            diagnostics.mkdir()
            (diagnostics / "merged-registry.json").write_text(json.dumps({
                "format": "bento/html-registry/v1",
                "document": {}, "assets": {}, "fonts": {}, "equations": {},
                "figures": {}, "tables": {}, "charts": {},
                "protected": {"slideIds": [], "elementIds": [], "requiredText": []},
            }), encoding="utf-8")
        (self.root / "deck.yaml").write_text(
            yaml.safe_dump(state, allow_unicode=True, sort_keys=False), encoding="utf-8",
        )
        return (output / "presentation.final.bento.html").read_bytes(), document

    def test_dry_run_is_side_effect_free_and_actual_migration_is_idempotent(self) -> None:
        before = (self.root / "deck.yaml").read_bytes()
        state = load_state(self.root)
        migrated, report, manifest = migrate_v1_state(self.root, state, dry_run=True)
        self.assertEqual(report["toSchemaVersion"], 2)
        self.assertEqual(migrated["authoring"]["mode"], "modular")
        self.assertEqual(manifest["items"][0]["path"], "sources/private/paper.pdf")
        self.assertEqual((self.root / "deck.yaml").read_bytes(), before)
        self.assertFalse((self.root / "deck.v1.backup.yaml").exists())
        self.assertFalse((self.root / "sources/source-manifest.yaml").exists())

        command_migrate(self.root, state, dry_run=False, report_path=None)
        migrated_state = load_state(self.root)
        self.assertEqual(migrated_state["schemaVersion"], 2)
        self.assertEqual(migrated_state["project"]["kind"], "paper_explanation")
        self.assertEqual(migrated_state["outputs"]["generatedHtml"], state["outputs"]["generatedHtml"])
        self.assertEqual((self.root / "deck.v1.backup.yaml").read_bytes(), before)
        self.assertTrue((self.root / "sources/source-manifest.yaml").is_file())
        command_migrate(self.root, migrated_state, dry_run=False, report_path=None)
        self.assertEqual(load_state(self.root), migrated_state)

    def test_late_stage_snapshots_registry_without_changing_final(self) -> None:
        final_before, _ = self.prepare_late_stage()
        command_migrate(self.root, load_state(self.root), dry_run=False, report_path=None)
        state = load_state(self.root)
        self.assertEqual(state["workflow"]["stage"], "bento_finalization")
        self.assertTrue(state["migration"]["lateStageCompatibility"])
        self.assertIsNone(state["outputs"]["authoringHtml"])
        self.assertEqual((self.root / "output/presentation.final.bento.html").read_bytes(), final_before)
        final_registry = json.loads((self.root / state["outputs"]["finalRegistry"]).read_text(encoding="utf-8"))
        self.assertEqual(final_registry["format"], REGISTRY_V2)
        baseline = state["validation"]["finalBaseline"]
        self.assertEqual(baseline["registryRevision"], registry_revision(final_registry))
        self.assertEqual(
            json.loads((self.root / baseline["registryPath"]).read_text(encoding="utf-8")),
            final_registry,
        )

    def test_late_stage_missing_registry_leaves_original_state_unchanged(self) -> None:
        self.prepare_late_stage(registry=False)
        before = (self.root / "deck.yaml").read_bytes()
        with self.assertRaisesRegex(WorkflowError, "requires merged registry"):
            command_migrate(self.root, load_state(self.root), dry_run=False, report_path=None)
        self.assertEqual((self.root / "deck.yaml").read_bytes(), before)
        self.assertFalse((self.root / "deck.v1.backup.yaml").exists())

    def test_v2_rejects_path_aliases_bad_sidecars_and_manifest_escape(self) -> None:
        migrated, _, manifest = migrate_v1_state(self.root, load_state(self.root), dry_run=True)
        (self.root / "sources/source-manifest.yaml").write_text(
            yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8",
        )
        duplicate = copy.deepcopy(migrated)
        duplicate["outputs"]["authoringHtml"] = duplicate["outputs"]["generatedHtml"]
        with self.assertRaisesRegex(WorkflowError, "must be distinct"):
            validate_state(self.root, duplicate)
        sidecar = copy.deepcopy(migrated)
        sidecar["outputs"]["authoringJson"] = "output/wrong.json"
        with self.assertRaisesRegex(WorkflowError, "authoringJson"):
            validate_state(self.root, sidecar)
        escaped = copy.deepcopy(migrated)
        escaped["sources"]["manifest"] = "../outside.yaml"
        with self.assertRaisesRegex(WorkflowError, "escapes"):
            validate_state(self.root, escaped)


if __name__ == "__main__":
    unittest.main()
