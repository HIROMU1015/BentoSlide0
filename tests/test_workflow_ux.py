from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from bento_converter.html_document import extract_bento_doc, load_html
from bento_converter.section_candidate import section_candidate
from bento_converter.segment import merge_segment, slide_hashes
from scripts.deck_workflow import (
    WorkflowError,
    atomic_write_state,
    command_approve_current,
    command_approve_plan,
    command_advance,
    command_begin_section,
    command_complete_section,
    command_configure_sections,
    command_finish_current_section,
    command_capture_request,
    command_initialize,
    command_promote_current_section,
    command_reopen_current_section,
    command_submit_plan,
    load_state,
    user_status_summary,
    valid_actions,
    workspace_route,
)
from tests.test_section_approval import html as sample_html, registry as sample_registry
from tests.test_segment import registry as segment_registry


ROOT = Path(__file__).resolve().parents[1]


class WorkflowUxUnitTests(unittest.TestCase):
    def test_request_capture_and_unambiguous_source_registration_hide_manifest_mechanics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for directory in ("workflow", "sources/private", "planning"):
                (root / directory).mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / "workflow/deck.schema.json", root / "workflow/deck.schema.json")
            shutil.copy2(ROOT / "workflow/deck.v1.schema.json", root / "workflow/deck.v1.schema.json")
            shutil.copy2(ROOT / "tests/fixtures/deck_v2.initialized.yaml", root / "deck.yaml")
            (root / "sources/source-manifest.yaml").write_text(
                "schemaVersion: 1\nauthorityMode: single\nitems: []\n", encoding="utf-8",
            )
            (root / "sources/private/研究メモ.md").write_text("evidence", encoding="utf-8")
            (root / "REQUEST.md").write_text("# Request\n", encoding="utf-8")
            state = load_state(root)
            command_capture_request(root, state, text="この研究メモを8枚で説明する")
            command_initialize(root, state)
            updated = load_state(root)
            manifest = yaml.safe_load((root / "sources/source-manifest.yaml").read_text(encoding="utf-8"))
            request_text = (root / "REQUEST.md").read_text(encoding="utf-8")
        self.assertIn("8枚", request_text)
        self.assertEqual(updated["workflow"]["stage"], "planning")
        self.assertEqual(len(manifest["items"]), 1)
        self.assertEqual(manifest["items"][0]["role"], "primary")

    def test_route_mapping_is_deterministic_and_complete(self) -> None:
        state = yaml.safe_load((ROOT / "tests/fixtures/deck_v2.initialized.yaml").read_text(encoding="utf-8"))
        expected = {
            "initialized": "none", "planning": "html-preview", "awaiting_plan_approval": "none",
            "html_authoring": "html-preview", "html_review": "html-preview",
            "ready_for_conversion": "none", "converting": "none", "bento_validation": "none",
            "bento_authoring": "authoring-editor", "content_review": "authoring-editor",
            "bento_finalization": "final-editor", "complete": "final-viewer", "blocked": "none",
        }
        for stage, route in expected.items():
            with self.subTest(stage=stage):
                state["workflow"]["stage"] = stage
                self.assertEqual(workspace_route(state), route)
                self.assertEqual(user_status_summary(state)["route"], route)
        state["workflow"]["stage"] = "html_review"
        state["workflow"]["currentSection"] = "intro"
        state["sections"] = {"intro": {"status": "html_review"}}
        self.assertEqual(valid_actions(state), ["approve-current", "edit-current"])
        state["sections"]["intro"]["status"] = "bento_integration"
        self.assertIn("promote-current-section", valid_actions(state))

    def test_section_candidate_keeps_only_selected_dom_and_registry_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "deck.preview.html"
            source.write_text(sample_html(), encoding="utf-8")
            value = sample_registry()
            candidate, projected, slide_ids = section_candidate(source, value, section_id="method")
        self.assertEqual(slide_ids, ["method-1"])
        self.assertIn('data-section-id="method"', candidate)
        self.assertNotIn('data-section-id="introduction"', candidate)
        self.assertEqual(projected["assets"], {})
        self.assertEqual(projected["charts"], {})

    def test_ordered_segment_insert_and_section_replace_preserve_unrelated_hashes(self) -> None:
        document = extract_bento_doc(load_html(ROOT / "demo.bento.html"))
        registry = segment_registry()
        incoming = json.loads(json.dumps(document, ensure_ascii=False))
        incoming["slides"] = [incoming["slides"][0]]
        incoming["slides"][0]["id"] = "inserted"
        for index, element in enumerate(incoming["slides"][0]["elements"]):
            element["id"] = f"inserted-{index}"
            element.pop("equationId", None)
            element.pop("latexSource", None)
        before = slide_hashes(document)
        merged, merged_registry, _ = merge_segment(
            document, registry, incoming, registry,
            operation="insert-before", anchor_slide_id=document["slides"][1]["id"],
        )
        self.assertEqual([slide["id"] for slide in merged["slides"]][1], "inserted")
        self.assertEqual({key: slide_hashes(merged)[key] for key in before}, before)
        replacement = json.loads(json.dumps(merged, ensure_ascii=False))
        replacement["slides"] = [replacement["slides"][1]]
        replacement["slides"][0]["elements"][0]["html"] = "replacement"
        replaced, _, _ = merge_segment(
            merged, merged_registry, replacement, merged_registry,
            operation="replace-section", target_slide_ids=["inserted"],
        )
        for slide_id, digest in slide_hashes(merged).items():
            if slide_id != "inserted":
                self.assertEqual(slide_hashes(replaced)[slide_id], digest)


@unittest.skipUnless(os.environ.get("BENTO_BROWSER_TEST") == "1", "requires Chromium")
class RollingSectionBrowserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for directory in ("workflow", "sources/private", "planning", "deck/assets", "output"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        for relative in (
            "workflow/deck.schema.json", "workflow/deck.v1.schema.json", "Bento_Slides.base.bento.html",
            "planning/explanation-policy.md", "planning/story-outline.md", "planning/slide-plan.md",
        ):
            shutil.copy2(ROOT / relative, self.root / relative)
        for name in ("explanation-policy.md", "story-outline.md", "slide-plan.md"):
            (self.root / "planning" / name).write_text("# Plan\n\nSubstantive workflow plan.\n", encoding="utf-8")
        shutil.copy2(ROOT / "tests/fixtures/deck_v2.initialized.yaml", self.root / "deck.yaml")
        (self.root / "REQUEST.md").write_text("# Request\n\nBuild a demo.\n", encoding="utf-8")
        (self.root / "sources/private/spec.md").write_text("source", encoding="utf-8")
        (self.root / "sources/source-manifest.yaml").write_text(yaml.safe_dump({
            "schemaVersion": 1, "authorityMode": "single",
            "items": [{"id": "spec", "path": "sources/private/spec.md", "type": "document", "role": "primary"}],
        }, sort_keys=False), encoding="utf-8")
        deck_html = sample_html().replace("assets/図 表.png", "assets/fixture.svg")
        (self.root / "deck/deck.preview.html").write_text(deck_html, encoding="utf-8")
        registry_value = sample_registry()
        registry_value["assets"]["plot"]["path"] = "assets/fixture.svg"
        registry_value["document"] = {
            "title": "Rolling demo", "modified": "2026-08-08T00:00:00Z",
            "theme": {"background": "#fff", "color": "#111", "accent": "#2563eb", "fontFamily": "Arial"},
        }
        (self.root / "deck/deck.registry.json").write_text(
            json.dumps(registry_value, ensure_ascii=False), encoding="utf-8",
        )
        (self.root / "deck/assets/fixture.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><rect width="40" height="40" fill="blue"/></svg>',
            encoding="utf-8",
        )
        state = load_state(self.root)
        state["workflow"].update(stage="planning", status="in_progress", owner="work", sourceOfTruth="planning")
        state["project"]["primarySource"] = "sources/private/spec.md"
        atomic_write_state(self.root, state)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_promote_accept_advance_and_reopen_without_touching_generated_or_final(self) -> None:
        state = load_state(self.root)
        command_configure_sections(self.root, state, ["method", "introduction"])
        command_submit_plan(self.root, state)
        command_approve_plan(self.root, state)
        command_begin_section(self.root, state, "method")
        command_complete_section(self.root, state, "method")
        command_approve_current(self.root, state)
        state = load_state(self.root)
        command_advance(self.root, state, browser_executable=None, browser_check=False)
        state = load_state(self.root)
        self.assertEqual(state["sections"]["method"]["status"], "bento_authoring")
        self.assertEqual(state["sections"]["method"]["canonical"], "bento")
        authoring = extract_bento_doc(load_html(self.root / state["outputs"]["authoringHtml"]))
        self.assertEqual([slide["id"] for slide in authoring["slides"]], ["method-1"])
        self.assertFalse((self.root / state["outputs"]["generatedHtml"]).exists())
        self.assertFalse((self.root / state["outputs"]["finalHtml"]).exists())
        command_finish_current_section(self.root, state)
        state = load_state(self.root)
        self.assertEqual(state["sections"]["method"]["status"], "accepted")
        self.assertEqual(state["workflow"]["currentSection"], "introduction")
        command_complete_section(self.root, state, "introduction")
        command_approve_current(self.root, state)
        state = load_state(self.root)
        command_advance(self.root, state, browser_executable=None, browser_check=False)
        state = load_state(self.root)
        authoring = extract_bento_doc(load_html(self.root / state["outputs"]["authoringHtml"]))
        self.assertEqual([slide["id"] for slide in authoring["slides"]], ["method-1", "導入-1"])
        command_finish_current_section(self.root, state)
        state = load_state(self.root)
        self.assertEqual(state["workflow"]["stage"], "content_review")
        self.assertTrue(all(entry["status"] == "accepted" for entry in state["sections"].values()))
        command_reopen_current_section(self.root, state, section_id="method", via="bento")
        state = load_state(self.root)
        self.assertEqual(state["workflow"]["stage"], "bento_authoring")
        self.assertEqual(state["sections"]["method"]["status"], "bento_authoring")

    def test_changed_html_aborts_promotion_without_authoring_or_state_change(self) -> None:
        state = load_state(self.root)
        command_configure_sections(self.root, state, ["method", "introduction"])
        command_submit_plan(self.root, state)
        command_approve_plan(self.root, state)
        command_begin_section(self.root, state, "method")
        command_complete_section(self.root, state, "method")
        command_approve_current(self.root, state)
        before = (self.root / "deck.yaml").read_bytes()
        source = self.root / "deck/deck.preview.html"
        source.write_text(source.read_text(encoding="utf-8").replace("Method", "Changed"), encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "changed after approval"):
            command_promote_current_section(
                self.root, load_state(self.root), browser_executable=None, browser_check=False,
            )
        self.assertEqual((self.root / "deck.yaml").read_bytes(), before)
        self.assertFalse((self.root / "output/presentation.authoring.bento.html").exists())


if __name__ == "__main__":
    unittest.main()
