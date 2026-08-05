from __future__ import annotations

import hashlib
import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import yaml

from bento_converter.html_document import (
    embed_bento_doc,
    extract_bento_doc,
    load_html,
    runtime_fingerprint,
    serialize_bento_doc,
)
from bento_converter.work_editor_storage import document_revision, protected_content_fingerprint
from scripts.apply_bento_final_edits import (
    PATCH_FORMAT,
    FinalEditError,
    apply_final_edits,
    apply_patch_document,
    main,
)
from scripts.deck_workflow import atomic_write_state


ROOT = Path(__file__).resolve().parents[1]


class FastFinalEditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "presentation.generated.bento.html"
        self.target = self.root / "presentation.final.bento.html"
        self.source.write_bytes((ROOT / "demo.bento.html").read_bytes())
        self.target.write_bytes(self.source.read_bytes())
        self.source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.before_html = load_html(self.target)
        self.before = extract_bento_doc(self.before_html)
        self.slide = self.before["slides"][0]
        self.shape = next(element for element in self.slide["elements"] if element["type"] == "shape")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def patch(self, **updates) -> dict:
        patch = {"format": PATCH_FORMAT}
        patch.update(updates)
        return patch

    def standard_context(self, *, stage: str = "bento_finalization", registry: bool = True) -> dict:
        workflow = self.root / "workflow"
        artifacts = self.root / "artifacts"
        diagnostics = artifacts / "diagnostics"
        revisions = artifacts / "revisions"
        for directory in (workflow, diagnostics, revisions):
            directory.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "workflow/deck.schema.json", workflow / "deck.schema.json")

        source = artifacts / "custom.generated.bento.html"
        target = artifacts / "custom.final.bento.html"
        source_json = artifacts / "custom.generated.bento.json"
        target_json = artifacts / "custom.final.bento.json"
        baseline = revisions / "custom.final.baseline.bento.json"
        source.write_text(self.before_html, encoding="utf-8")
        target.write_text(self.before_html, encoding="utf-8")
        serialized = serialize_bento_doc(self.before) + "\n"
        source_json.write_text(serialized, encoding="utf-8")
        target_json.write_text(serialized, encoding="utf-8")
        baseline.write_text(serialized, encoding="utf-8")
        registry_path = diagnostics / "merged-registry.json"
        if registry:
            registry_path.write_text(json.dumps({
                "protected": {
                    "slideIds": [self.slide["id"]],
                    "elementIds": ["slide-1-title"],
                    "requiredText": ["GPTが座標まで設計する"],
                },
                "equations": {}, "figures": {}, "charts": {}, "tables": {},
            }, ensure_ascii=False), encoding="utf-8")

        state = yaml.safe_load((ROOT / "deck.yaml").read_text(encoding="utf-8"))
        owner_source = {
            "bento_validation": ("codex", "generated"),
            "bento_finalization": ("work", "final"),
            "complete": ("codex", "final"),
        }
        owner, source_of_truth = owner_source[stage]
        state["workflow"].update(
            stage=stage, status="in_progress", owner=owner, sourceOfTruth=source_of_truth,
        )
        state["handoff"]["readyForFinalEditing"] = stage == "bento_finalization"
        state["outputs"] = {
            "generatedHtml": "artifacts/custom.generated.bento.html",
            "generatedJson": "artifacts/custom.generated.bento.json",
            "finalHtml": "artifacts/custom.final.bento.html",
            "finalJson": "artifacts/custom.final.bento.json",
        }
        state["validation"]["finalBaseline"] = {
            "path": "artifacts/revisions/custom.final.baseline.bento.json",
            "documentRevision": document_revision(self.before),
            "protectedContentFingerprint": protected_content_fingerprint(self.before),
        }
        atomic_write_state(self.root, state)

        patch_path = self.root / "final-edit.json"
        patch_path.write_text(json.dumps(self.patch(elementEdits=[{
            "slideId": self.slide["id"],
            "elementId": self.shape["id"],
            "set": {"opacity": 0.8},
        }])), encoding="utf-8")
        return {
            "source": source, "target": target, "targetJson": target_json,
            "registry": registry_path, "baseline": baseline, "patch": patch_path,
        }

    def run_standard_cli(self, patch: Path, *extra: str) -> tuple[int, str]:
        stderr = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            exit_code = main(["--root", str(self.root), "--patch", patch.name, *extra])
        return exit_code, stderr.getvalue()

    def test_batch_saves_geometry_style_background_and_z_order_once(self) -> None:
        original_ids = [element["id"] for element in self.slide["elements"]]
        patch = self.patch(
            baseRevision=document_revision(self.before),
            slideEdits=[{"slideId": self.slide["id"], "set": {"background": "#EEF2FF"}}],
            elementEdits=[{
                "slideId": self.slide["id"],
                "elementId": self.shape["id"],
                "set": {"x": 600, "w": self.shape["w"] * 1.5, "fill": "#2563EB"},
            }],
            zOrders=[{"slideId": self.slide["id"], "elementIds": list(reversed(original_ids))}],
        )

        result = apply_final_edits(
            source=self.source, target=self.target, registry=None, patch=patch,
        )

        after_html = load_html(self.target)
        after = extract_bento_doc(after_html)
        after_slide = after["slides"][0]
        after_shape = next(element for element in after_slide["elements"] if element["id"] == self.shape["id"])
        sidecar = self.root / "presentation.final.bento.json"
        self.assertTrue(result["saved"])
        self.assertEqual(result["validation"], "pass")
        self.assertEqual(result["backupCount"], 1)
        self.assertTrue(result["protectedContentFingerprintUnchanged"])
        self.assertTrue(result["htmlJsonEqual"])
        self.assertEqual(after_slide["background"], "#EEF2FF")
        self.assertEqual(after_shape["x"], 600)
        self.assertEqual(after_shape["w"], self.shape["w"] * 1.5)
        self.assertEqual(after_shape["fill"], "#2563EB")
        self.assertEqual([element["id"] for element in after_slide["elements"]], list(reversed(original_ids)))
        self.assertEqual(json.loads(sidecar.read_text(encoding="utf-8")), after)
        self.assertEqual(runtime_fingerprint(self.before_html), runtime_fingerprint(after_html))
        self.assertEqual(protected_content_fingerprint(self.before), protected_content_fingerprint(after))
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.source_hash)

    def test_protected_content_field_is_rejected_before_save(self) -> None:
        before_bytes = self.target.read_bytes()
        patch = self.patch(elementEdits=[{
            "slideId": self.slide["id"], "elementId": "slide-1-title", "set": {"html": "Changed"},
        }])
        with self.assertRaises(FinalEditError):
            apply_final_edits(source=self.source, target=self.target, registry=None, patch=patch)
        self.assertEqual(self.target.read_bytes(), before_bytes)

    def test_z_order_requires_every_element_exactly_once(self) -> None:
        patch = self.patch(zOrders=[{
            "slideId": self.slide["id"],
            "elementIds": [element["id"] for element in self.slide["elements"][:-1]],
        }])
        with self.assertRaisesRegex(FinalEditError, "every existing element"):
            apply_patch_document(self.before, patch)

    def test_dry_run_validates_without_changing_target_or_creating_backup(self) -> None:
        before_bytes = self.target.read_bytes()
        patch = self.patch(elementEdits=[{
            "slideId": self.slide["id"], "elementId": self.shape["id"], "set": {"h": self.shape["h"] + 5},
        }])
        result = apply_final_edits(
            source=self.source, target=self.target, registry=None, patch=patch, dry_run=True,
        )
        self.assertFalse(result["saved"])
        self.assertTrue(result["dryRun"])
        self.assertEqual(result["validation"], "pass")
        self.assertEqual(result["backupCount"], 0)
        self.assertEqual(self.target.read_bytes(), before_bytes)
        self.assertFalse((self.root / "revisions").exists())

    def test_stale_base_revision_is_rejected(self) -> None:
        patch = self.patch(
            baseRevision="sha256:stale",
            elementEdits=[{
                "slideId": self.slide["id"], "elementId": self.shape["id"], "set": {"x": 1},
            }],
        )
        with self.assertRaisesRegex(FinalEditError, "stale"):
            apply_final_edits(source=self.source, target=self.target, registry=None, patch=patch)

    def test_no_op_does_not_create_a_backup(self) -> None:
        patch = self.patch(elementEdits=[{
            "slideId": self.slide["id"], "elementId": self.shape["id"], "set": {"x": self.shape["x"]},
        }])
        result = apply_final_edits(source=self.source, target=self.target, registry=None, patch=patch)
        self.assertFalse(result["saved"])
        self.assertTrue(result["noOp"])
        self.assertEqual(result["backupCount"], 0)
        self.assertFalse((self.root / "revisions").exists())

    def test_cli_accepts_explicit_paths_and_writes_report(self) -> None:
        patch_path = self.root / "edit.json"
        report_path = self.root / "report.json"
        patch_path.write_text(json.dumps(self.patch(elementEdits=[{
            "slideId": self.slide["id"], "elementId": self.shape["id"], "set": {"opacity": 0.8},
        }])), encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            exit_code = main([
                "--root", str(self.root),
                "--patch", patch_path.name,
                "--source", self.source.name,
                "--target", self.target.name,
                "--report", report_path.name,
            ])
        self.assertEqual(exit_code, 0)
        self.assertTrue(report_path.is_file())
        self.assertTrue(json.loads(report_path.read_text(encoding="utf-8"))["saved"])

    def test_standard_deck_paths_resolve_custom_outputs_registry_and_baseline(self) -> None:
        context = self.standard_context()
        report = self.root / "artifacts/reports/fast-edit.json"
        exit_code, error = self.run_standard_cli(
            context["patch"], "--report", "artifacts/reports/fast-edit.json",
        )
        self.assertEqual((exit_code, error), (0, ""))
        edited = extract_bento_doc(load_html(context["target"]))
        shape = next(element for slide in edited["slides"] for element in slide["elements"] if element["id"] == self.shape["id"])
        self.assertEqual(shape["opacity"], 0.8)
        self.assertEqual(json.loads(context["targetJson"].read_text(encoding="utf-8")), edited)
        self.assertTrue(json.loads(report.read_text(encoding="utf-8"))["protectedContentFingerprintUnchanged"])

    def test_standard_path_rejects_non_finalization_stage(self) -> None:
        context = self.standard_context(stage="bento_validation")
        before = context["target"].read_bytes()
        exit_code, error = self.run_standard_cli(context["patch"])
        self.assertEqual(exit_code, 2)
        self.assertIn("requires 'bento_finalization' or 'complete'", error)
        self.assertEqual(context["target"].read_bytes(), before)

    def test_standard_path_rejects_missing_registry(self) -> None:
        context = self.standard_context(registry=False)
        before = context["target"].read_bytes()
        exit_code, error = self.run_standard_cli(context["patch"])
        self.assertEqual(exit_code, 2)
        self.assertIn("Required merged registry does not exist", error)
        self.assertEqual(context["target"].read_bytes(), before)

    def test_standard_path_rejects_final_that_violates_immutable_baseline(self) -> None:
        context = self.standard_context()
        tampered = json.loads(json.dumps(self.before))
        next(
            element for slide in tampered["slides"] for element in slide["elements"]
            if element["id"] == "slide-1-title"
        )["html"] = "Externally tampered content"
        context["target"].write_text(
            embed_bento_doc(self.before_html, tampered), encoding="utf-8",
        )
        context["targetJson"].write_text(serialize_bento_doc(tampered) + "\n", encoding="utf-8")
        before = context["target"].read_bytes()
        exit_code, error = self.run_standard_cli(context["patch"])
        self.assertEqual(exit_code, 2)
        self.assertIn("Validation failed", error)
        self.assertEqual(context["target"].read_bytes(), before)

    def test_report_collision_is_rejected_before_final_save(self) -> None:
        context = self.standard_context()
        before = context["target"].read_bytes()
        collisions = (
            "artifacts/custom.generated.bento.html",
            "artifacts/custom.final.bento.html",
            "artifacts/custom.generated.bento.json",
            "artifacts/custom.final.bento.json",
            "artifacts/diagnostics/merged-registry.json",
            "artifacts/revisions/custom.final.baseline.bento.json",
            "artifacts/revisions/new-report.json",
            "final-edit.json",
            "deck.yaml",
        )
        for collision in collisions:
            with self.subTest(collision=collision):
                exit_code, error = self.run_standard_cli(
                    context["patch"], "--report", collision,
                )
                self.assertEqual(exit_code, 2)
                self.assertIn("report", error)
                self.assertEqual(context["target"].read_bytes(), before)
        self.assertEqual(context["target"].read_bytes(), before)
        self.assertEqual(extract_bento_doc(load_html(context["target"])), self.before)


if __name__ == "__main__":
    unittest.main()
