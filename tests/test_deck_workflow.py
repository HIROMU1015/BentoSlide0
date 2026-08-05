from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from bento_converter.errors import ValidationError
from bento_converter.html_document import embed_bento_doc, extract_bento_doc, load_html, serialize_bento_doc
from scripts.deck_workflow import (
    WorkflowError,
    atomic_write_state,
    command_approve_chapter,
    command_approve_final,
    command_approve_plan,
    command_begin_authoring,
    command_begin_chapter,
    command_begin_finalization,
    command_block,
    command_complete,
    command_complete_chapter,
    command_configure_chapters,
    command_initialize,
    command_mark_converted,
    command_migrate,
    command_prepare_conversion,
    command_resume,
    command_submit_plan,
    discover_source_candidates,
    load_state,
    validate_chapters,
    validate_state,
)


ROOT = Path(__file__).resolve().parents[1]


class DeckWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for directory in ("workflow", "sources/private", "planning", "chapters", "output/diagnostics"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        for relative in (
            "deck.yaml", "REQUEST.md", "workflow/deck.schema.json", "workflow/deck.v1.schema.json",
            "planning/explanation-policy.md", "planning/story-outline.md", "planning/slide-plan.md",
            "planning/decisions.md", "planning/work-log.md",
        ):
            shutil.copy2(ROOT / relative, self.root / relative)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def state(self) -> dict:
        return load_state(self.root)

    def write_state(self, state: dict) -> None:
        atomic_write_state(self.root, state)

    def add_source(self, name: str = "論文.pdf") -> Path:
        path = self.root / "sources/private" / name
        path.write_bytes(b"%PDF-1.4\nfixture\n")
        return path

    def fill_plan(self) -> None:
        (self.root / "planning/explanation-policy.md").write_text("# Policy\n\nExplain the verified contribution.\n", encoding="utf-8")
        (self.root / "planning/story-outline.md").write_text("# Story\n\nProblem to evidence.\n", encoding="utf-8")
        (self.root / "planning/slide-plan.md").write_text("# Plan\n\nTwo approved chapters.\n", encoding="utf-8")

    def add_chapter(self, chapter_id: str, *, slide_id: str | None = None, duplicate_element: bool = False, latex: str = "E=mc^2") -> None:
        slide = slide_id or f"{chapter_id}-slide"
        duplicate = '<p data-bento-id="title">duplicate</p>' if duplicate_element else ""
        html = f"""<!doctype html><html><body><section class="slide" data-slide-id="{slide}">
<h1 data-bento-id="title">Title</h1>{duplicate}
<div data-bento-id="equation" data-equation-id="energy" data-latex="{latex}">E = mc2</div>
</section></body></html>"""
        registry = {
            "format": "bento/html-registry/v1", "chapterId": chapter_id,
            "equations": {"energy": {"latex": "E=mc^2", "usedOnSlides": [slide]}},
            "protected": {"slideIds": [slide], "elementIds": ["title"], "requiredText": []},
        }
        (self.root / f"chapters/{chapter_id}.preview.html").write_text(html, encoding="utf-8")
        (self.root / f"chapters/{chapter_id}.registry.json").write_text(json.dumps(registry), encoding="utf-8")

    def plan_to_authoring(self, chapters: tuple[str, ...] = ("chapter-01",)) -> dict:
        self.add_source()
        state = self.state()
        command_initialize(self.root, state)
        self.fill_plan()
        state = self.state()
        command_configure_chapters(self.root, state, chapters)
        state = self.state()
        command_submit_plan(self.root, state)
        state = self.state()
        command_approve_plan(self.root, state)
        return self.state()

    def author_and_approve(self, state: dict, chapter_id: str) -> dict:
        command_begin_chapter(self.root, state, chapter_id)
        self.add_chapter(chapter_id)
        state = self.state()
        command_complete_chapter(self.root, state, chapter_id)
        state = self.state()
        command_approve_chapter(self.root, state, chapter_id)
        return self.state()

    def prepare_output_bundle(self, state: dict, *, existing_final: bool) -> tuple[str, str | None]:
        output = self.root / "output"
        generated_html = output / "presentation.generated.bento.html"
        generated_json = output / "presentation.generated.bento.json"
        source_html = load_html(ROOT / "demo.bento.html")
        source_doc = extract_bento_doc(source_html)
        generated_html.write_text(source_html, encoding="utf-8")
        generated_json.write_text(serialize_bento_doc(source_doc) + "\n", encoding="utf-8")
        (output / "conversion-report.json").write_text(json.dumps({"summary": {"criticalElementFail": 0, "unresolvedLocalResourceReferences": 0}}), encoding="utf-8")
        diagnostics = output / "diagnostics"
        (diagnostics / "computed-layout.json").write_text("{}\n", encoding="utf-8")
        (diagnostics / "merged-registry.json").write_text(json.dumps({
            "format": "bento/html-registry/v2", "unitId": "deck", "sources": {},
            "document": {}, "assets": {}, "fonts": {},
            "equations": {"hamiltonian_split": {"latex": "H = H_0 + \\alpha H_1"}},
            "figures": {}, "tables": {}, "charts": {},
            "protected": {"slideIds": [], "elementIds": [], "requiredText": []},
        }), encoding="utf-8")
        (diagnostics / "resource-scan.json").write_text(json.dumps({"passed": True, "unresolved": []}), encoding="utf-8")
        (diagnostics / "browser-check.json").write_text(json.dumps({"serialize_roundtrip": True}), encoding="utf-8")
        generated_hash = hashlib.sha256(generated_html.read_bytes()).hexdigest()
        final_hash = None
        if existing_final:
            final_doc = copy.deepcopy(source_doc)
            next(element for slide in final_doc["slides"] for element in slide["elements"] if element["type"] == "shape")["x"] += 7
            final_html = embed_bento_doc(source_html, final_doc)
            (output / "presentation.final.bento.html").write_text(final_html, encoding="utf-8")
            (output / "presentation.final.bento.json").write_text(serialize_bento_doc(final_doc) + "\n", encoding="utf-8")
            final_hash = hashlib.sha256((output / "presentation.final.bento.html").read_bytes()).hexdigest()
        return generated_hash, final_hash

    def ready_for_conversion(self) -> dict:
        state = self.plan_to_authoring()
        state = self.author_and_approve(state, "chapter-01")
        self.assertEqual(state["workflow"]["stage"], "ready_for_conversion")
        command_prepare_conversion(self.root, state)
        return self.state()

    def test_schema_rejects_wrong_stage_owner_and_path_escape(self) -> None:
        previous_template = self.state()
        previous_template["workflow"].pop("blockedFrom")
        previous_template["validation"].pop("finalBaseline")
        validate_state(self.root, previous_template)
        state = self.state()
        state["workflow"]["owner"] = "codex"
        with self.assertRaisesRegex(WorkflowError, "owner"):
            validate_state(self.root, state)
        state = self.state()
        state["outputs"]["generatedHtml"] = "../outside.bento.html"
        with self.assertRaisesRegex(WorkflowError, "escapes"):
            validate_state(self.root, state)
        state = self.state()
        state["preview"]["currentUrl"] = "http://127.0.0.1:70000/"
        with self.assertRaisesRegex(WorkflowError, "invalid port"):
            validate_state(self.root, state)
        state = self.state()
        state["outputs"]["finalHtml"] = state["outputs"]["generatedHtml"]
        with self.assertRaisesRegex(WorkflowError, "must be distinct"):
            validate_state(self.root, state)
        state = self.state()
        state["outputs"]["generatedJson"] = "output/not-the-generated-sidecar.json"
        with self.assertRaisesRegex(WorkflowError, "sidecar path"):
            validate_state(self.root, state)

    def test_blocked_state_records_machine_readable_reason(self) -> None:
        command_block(self.root, self.state(), "work", "Primary source decision is required")
        blocked = self.state()
        self.assertEqual(blocked["workflow"]["stage"], "blocked")
        self.assertEqual(blocked["workflow"]["owner"], "work")
        self.assertEqual(blocked["workflow"]["blockingReason"], "Primary source decision is required")
        self.assertEqual(blocked["workflow"]["blockedFrom"]["stage"], "initialized")
        command_resume(self.root, blocked)
        resumed = self.state()
        self.assertEqual(resumed["workflow"]["stage"], "initialized")
        self.assertIsNone(resumed["workflow"]["blockedFrom"])

    def test_resume_revalidates_files_before_restoring_html_review(self) -> None:
        state = self.plan_to_authoring()
        command_begin_chapter(self.root, state, "chapter-01")
        self.add_chapter("chapter-01")
        command_complete_chapter(self.root, self.state(), "chapter-01")
        command_block(self.root, self.state(), "work", "Review source needs repair")
        registry = self.root / "chapters/chapter-01.registry.json"
        registry.unlink()
        with self.assertRaisesRegex(WorkflowError, "registry does not exist"):
            command_resume(self.root, self.state())
        self.assertEqual(self.state()["workflow"]["stage"], "blocked")
        self.add_chapter("chapter-01")
        command_resume(self.root, self.state())
        resumed = self.state()
        self.assertEqual(resumed["workflow"]["stage"], "html_review")
        self.assertEqual(resumed["workflow"]["currentChapter"], "chapter-01")

    def test_atomic_write_uses_replace_and_roundtrips_unicode(self) -> None:
        state = self.state()
        state["project"]["title"] = "量子スライド"
        real_replace = os.replace
        with mock.patch("scripts.deck_workflow.os.replace", wraps=real_replace) as replace:
            atomic_write_state(self.root, state)
        replace.assert_called_once()
        self.assertEqual(self.state()["project"]["title"], "量子スライド")
        self.assertFalse(list(self.root.glob(".deck.*.yaml.tmp")))

    def test_primary_source_discovery_single_japanese_pdf(self) -> None:
        source = self.add_source("日本語 論文.pdf")
        selected, candidates = discover_source_candidates(self.root, self.state())
        self.assertEqual(selected, source.resolve())
        self.assertEqual(candidates, [source.resolve()])
        state = self.state()
        command_initialize(self.root, state)
        updated = self.state()
        self.assertEqual(updated["project"]["primarySource"], "sources/private/日本語 論文.pdf")
        self.assertEqual(updated["workflow"]["stage"], "planning")

    def test_primary_source_absent_and_multiple_require_decision(self) -> None:
        with self.assertRaisesRegex(WorkflowError, "No primary PDF"):
            discover_source_candidates(self.root, self.state())
        self.add_source("a.pdf")
        self.add_source("b.pdf")
        with self.assertRaisesRegex(WorkflowError, "Multiple PDF"):
            discover_source_candidates(self.root, self.state())
        state = self.state()
        state["project"]["primarySource"] = "sources/private/b.pdf"
        self.write_state(state)
        selected, _ = discover_source_candidates(self.root, self.state())
        self.assertEqual(selected.name, "b.pdf")

    def test_plan_requires_substantive_files_chapters_and_approval(self) -> None:
        self.add_source()
        state = self.state()
        command_initialize(self.root, state)
        with self.assertRaisesRegex(WorkflowError, "substantive"):
            command_submit_plan(self.root, self.state())
        self.fill_plan()
        with self.assertRaisesRegex(WorkflowError, "Register"):
            command_submit_plan(self.root, self.state())
        command_configure_chapters(self.root, self.state(), ("chapter-01", "chapter-02"))
        command_submit_plan(self.root, self.state())
        self.assertEqual(self.state()["workflow"]["stage"], "awaiting_plan_approval")
        command_approve_plan(self.root, self.state())
        updated = self.state()
        self.assertEqual(updated["workflow"]["stage"], "html_authoring")
        self.assertTrue(all(updated["approvals"][key] == "approved" for key in ("explanationPolicy", "storyOutline", "slidePlan")))

    def test_complete_chapter_requires_registry_and_rejects_duplicate_ids(self) -> None:
        state = self.plan_to_authoring()
        command_begin_chapter(self.root, state, "chapter-01")
        html = self.root / "chapters/chapter-01.preview.html"
        html.write_text('<section data-slide-id="slide"><h1 data-bento-id="title">T</h1></section>', encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "registry does not exist"):
            command_complete_chapter(self.root, self.state(), "chapter-01")
        self.add_chapter("chapter-01", duplicate_element=True)
        with self.assertRaisesRegex(WorkflowError, "Duplicate element IDs"):
            command_complete_chapter(self.root, self.state(), "chapter-01")
        self.add_chapter("chapter-01")
        registry_path = self.root / "chapters/chapter-01.registry.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        registry["protected"]["requiredText"] = ["text that is absent"]
        registry_path.write_text(json.dumps(registry), encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "required text"):
            command_complete_chapter(self.root, self.state(), "chapter-01")

    def test_equation_registry_mismatch_and_cross_chapter_slide_duplicates(self) -> None:
        state = self.plan_to_authoring(("chapter-01", "chapter-02"))
        command_begin_chapter(self.root, state, "chapter-01")
        self.add_chapter("chapter-01", latex="wrong")
        with self.assertRaisesRegex(WorkflowError, "data-latex"):
            command_complete_chapter(self.root, self.state(), "chapter-01")
        self.add_chapter("chapter-01", slide_id="shared")
        self.add_chapter("chapter-02", slide_id="shared")
        state = self.state()
        state["chapters"]["chapter-01"].update(status="complete", visualApproval="approved")
        state["chapters"]["chapter-02"].update(status="complete", visualApproval="approved")
        self.write_state(state)
        with self.assertRaisesRegex(WorkflowError, "across chapters"):
            validate_chapters(self.root, self.state(), require_complete=True)

    def test_visual_approval_gate_selects_next_then_ready(self) -> None:
        state = self.plan_to_authoring(("chapter-01", "chapter-02"))
        state = self.author_and_approve(state, "chapter-01")
        self.assertEqual(state["workflow"]["stage"], "html_authoring")
        self.assertEqual(state["workflow"]["currentChapter"], "chapter-02")
        self.assertEqual(state["chapters"]["chapter-02"]["status"], "authoring")
        self.add_chapter("chapter-02")
        command_complete_chapter(self.root, state, "chapter-02")
        command_approve_chapter(self.root, self.state(), "chapter-02")
        ready = self.state()
        self.assertEqual(ready["workflow"]["stage"], "ready_for_conversion")
        self.assertTrue(ready["handoff"]["readyForCodex"])

    def test_prepare_conversion_refuses_incomplete_chapter(self) -> None:
        state = self.plan_to_authoring(("chapter-01", "chapter-02"))
        state["workflow"].update(stage="ready_for_conversion", status="ready", owner="codex", sourceOfTruth="chapters", currentChapter=None)
        state["handoff"]["readyForCodex"] = True
        self.write_state(state)
        with self.assertRaisesRegex(WorkflowError, "not complete"):
            command_prepare_conversion(self.root, self.state())

    def test_mark_converted_initializes_missing_final_without_reset(self) -> None:
        state = self.ready_for_conversion()
        generated_hash, _ = self.prepare_output_bundle(state, existing_final=False)
        command_mark_converted(self.root, self.state())
        updated = self.state()
        self.assertEqual(updated["workflow"]["stage"], "bento_validation")
        self.assertTrue((self.root / "output/presentation.final.bento.html").is_file())
        self.assertEqual(hashlib.sha256((self.root / "output/presentation.generated.bento.html").read_bytes()).hexdigest(), generated_hash)
        baseline = updated["validation"]["finalBaseline"]
        self.assertIsNotNone(baseline)
        self.assertTrue((self.root / baseline["path"]).is_file())
        self.assertRegex(baseline["protectedContentFingerprint"], r"^sha256:[0-9a-f]{64}$")

    def test_mark_converted_preserves_existing_final(self) -> None:
        state = self.ready_for_conversion()
        generated_hash, final_hash = self.prepare_output_bundle(state, existing_final=True)
        command_mark_converted(self.root, self.state())
        self.assertEqual(hashlib.sha256((self.root / "output/presentation.generated.bento.html").read_bytes()).hexdigest(), generated_hash)
        self.assertEqual(hashlib.sha256((self.root / "output/presentation.final.bento.html").read_bytes()).hexdigest(), final_hash)

    def test_v2_mark_converted_initializes_authoring_then_explicitly_hands_off(self) -> None:
        state = self.ready_for_conversion()
        command_migrate(self.root, state, dry_run=False, report_path=None)
        self.prepare_output_bundle(self.state(), existing_final=False)
        command_mark_converted(self.root, self.state())
        validated = self.state()
        self.assertEqual(validated["workflow"]["stage"], "bento_validation")
        self.assertTrue(validated["handoff"]["readyForBentoAuthoring"])
        self.assertFalse((self.root / validated["outputs"]["finalHtml"]).exists())
        for field in ("authoringHtml", "authoringJson", "authoringRegistry"):
            self.assertTrue((self.root / validated["outputs"][field]).is_file())
        command_begin_authoring(self.root, validated)
        authoring = self.state()
        self.assertEqual(authoring["workflow"]["stage"], "bento_authoring")
        self.assertEqual(authoring["workflow"]["sourceOfTruth"], "authoring")
        self.assertTrue(authoring["handoff"]["readyForBentoAuthoring"])

    def test_finalization_and_complete_require_explicit_final_approval(self) -> None:
        state = self.ready_for_conversion()
        self.prepare_output_bundle(state, existing_final=False)
        command_mark_converted(self.root, self.state())
        command_begin_finalization(self.root, self.state())
        with self.assertRaisesRegex(WorkflowError, "approval"):
            command_complete(self.root, self.state())
        command_approve_final(self.root, self.state())
        command_complete(self.root, self.state())
        completed = self.state()
        self.assertEqual(completed["workflow"]["stage"], "complete")
        self.assertEqual(completed["validation"]["finalStatus"], "pass")
        self.assertFalse(completed["handoff"]["readyForFinalEditing"])

    def test_final_validation_rejects_content_replacement_but_allows_layout(self) -> None:
        state = self.ready_for_conversion()
        self.prepare_output_bundle(state, existing_final=False)
        command_mark_converted(self.root, self.state())
        command_begin_finalization(self.root, self.state())
        final_html_path = self.root / "output/presentation.final.bento.html"
        final_json_path = self.root / "output/presentation.final.bento.json"
        final_html = load_html(final_html_path)
        document = extract_bento_doc(final_html)
        shape = next(element for slide in document["slides"] for element in slide["elements"] if element["type"] == "shape")
        shape["x"] += 3
        shape["fill"] = "#abcdef"
        final_html_path.write_bytes(embed_bento_doc(final_html, document).encode("utf-8"))
        final_json_path.write_text(serialize_bento_doc(document) + "\n", encoding="utf-8")
        command_approve_final(self.root, self.state())

        document = extract_bento_doc(load_html(final_html_path))
        text = next(element for slide in document["slides"] for element in slide["elements"] if element["type"] == "text")
        text["html"] = "externally replaced content"
        final_html_path.write_bytes(embed_bento_doc(load_html(final_html_path), document).encode("utf-8"))
        final_json_path.write_text(serialize_bento_doc(document) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "protected"):
            command_complete(self.root, self.state())

    def test_final_fingerprint_rejects_slide_structure_reordering(self) -> None:
        state = self.ready_for_conversion()
        self.prepare_output_bundle(state, existing_final=False)
        command_mark_converted(self.root, self.state())
        command_begin_finalization(self.root, self.state())
        final_html_path = self.root / "output/presentation.final.bento.html"
        final_json_path = self.root / "output/presentation.final.bento.json"
        final_html = load_html(final_html_path)
        document = extract_bento_doc(final_html)
        document["slides"].reverse()
        final_html_path.write_bytes(embed_bento_doc(final_html, document).encode("utf-8"))
        final_json_path.write_text(serialize_bento_doc(document) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "content/structure"):
            command_approve_final(self.root, self.state())


if __name__ == "__main__":
    unittest.main()
