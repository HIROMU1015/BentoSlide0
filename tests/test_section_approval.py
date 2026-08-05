from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from bento_converter.section_approval import compute_section_approval_evidence
from scripts.deck_workflow import (
    WorkflowError,
    atomic_write_state,
    command_approve_plan,
    command_approve_section,
    command_begin_section,
    command_complete_section,
    command_configure_sections,
    command_prepare_conversion,
    command_submit_plan,
    command_unlock_section,
    load_state,
    migrate_v1_state,
)


ROOT = Path(__file__).resolve().parents[1]


def registry() -> dict:
    return {
        "format": "bento/html-registry/v2",
        "unitId": "deck",
        "sources": {},
        "document": {"title": "日本語デッキ", "theme": "light"},
        "assets": {"plot": {"path": "assets/図 表.png"}},
        "fonts": {}, "equations": {}, "figures": {}, "tables": {}, "charts": {"unused": {"data": [1]}},
        "protected": {"slideIds": [], "elementIds": [], "requiredText": []},
    }


def html(css: str = ".slide{width:1280px;height:720px}") -> str:
    return f'''<!doctype html><html data-theme="light"><head><style>{css}</style></head><body>
<main data-bento-deck>
  <section class="slide" data-slide-id="導入-1" data-section-id="introduction">
    <img data-bento-id="plot-image" data-asset-id="plot" src="assets/図 表.png">
  </section>
  <section class="slide" data-slide-id="method-1" data-section-id="method">
    <div data-bento-id="method-text">Method</div>
  </section>
</main></body></html>'''


class SectionDigestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "日本語 資料"
        (self.root / "deck/assets").mkdir(parents=True)
        self.html_path = self.root / "deck/deck.preview.html"
        self.html_path.write_text(html(), encoding="utf-8")
        (self.root / "deck/assets/図 表.png").write_bytes(b"png-one")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def evidence(self, value: dict | None = None):
        return compute_section_approval_evidence(self.html_path, value or registry(), repository=self.root)

    def test_digest_tracks_section_dom_registry_assets_and_global_css(self) -> None:
        initial = self.evidence()
        self.assertEqual(initial["introduction"].slide_ids, ("導入-1",))
        self.assertIn("deck/assets/図 表.png", initial["introduction"].asset_hashes)

        unrelated = registry()
        unrelated["charts"]["unused"]["data"] = [999]
        self.assertEqual(self.evidence(unrelated)["introduction"].digest, initial["introduction"].digest)

        (self.root / "deck/assets/図 表.png").write_bytes(b"png-two")
        changed_asset = self.evidence()
        self.assertNotEqual(changed_asset["introduction"].digest, initial["introduction"].digest)
        self.assertEqual(changed_asset["method"].digest, initial["method"].digest)

        self.html_path.write_text(html(".slide{width:1280px;height:720px;color:red}"), encoding="utf-8")
        changed_css = self.evidence()
        self.assertNotEqual(changed_css["introduction"].digest, changed_asset["introduction"].digest)
        self.assertNotEqual(changed_css["method"].digest, changed_asset["method"].digest)

    def test_duplicate_slides_and_missing_section_ids_are_rejected(self) -> None:
        self.html_path.write_text(
            "<section data-slide-id='same' data-section-id='a'></section>"
            "<section data-slide-id='same' data-section-id='b'></section>", encoding="utf-8",
        )
        with self.assertRaisesRegex(Exception, "Duplicate slide id"):
            self.evidence()


class SingleHtmlWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for directory in ("workflow", "sources/private", "sources", "planning", "deck/assets"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        for relative in ("workflow/deck.schema.json", "workflow/deck.v1.schema.json"):
            shutil.copy2(ROOT / relative, self.root / relative)
        shutil.copy2(ROOT / "deck.yaml", self.root / "deck.yaml")
        (self.root / "REQUEST.md").write_text("# Request\nCreate a research deck.\n", encoding="utf-8")
        (self.root / "sources/private/spec.md").write_text("evidence", encoding="utf-8")
        for filename in ("explanation-policy.md", "story-outline.md", "slide-plan.md"):
            (self.root / "planning" / filename).write_text("# Plan\nSubstantive content.\n", encoding="utf-8")
        (self.root / "deck/deck.preview.html").write_text(html(), encoding="utf-8")
        (self.root / "deck/deck.registry.json").write_text(json.dumps(registry(), ensure_ascii=False), encoding="utf-8")
        (self.root / "deck/assets/図 表.png").write_bytes(b"asset")

        # Build a clean v2 state first; the test manifest is a new-project
        # authority, not a file for the v1 migrator to overwrite.
        v1 = load_state(self.root)
        state, _, _ = migrate_v1_state(self.root, v1, dry_run=True)
        (self.root / "sources/source-manifest.yaml").write_text(yaml.safe_dump({
            "schemaVersion": 1,
            "authorityMode": "single",
            "items": [{"id": "spec", "path": "sources/private/spec.md", "type": "document", "role": "primary"}],
        }, sort_keys=False), encoding="utf-8")
        state["project"].update(kind="research_project", primarySource=None)
        state["sources"].update(manifest="sources/source-manifest.yaml", authorityMode="single")
        state["authoring"].update(mode="single", entryHtml="deck/deck.preview.html", registry="deck/deck.registry.json", currentSection=None)
        state["workflow"].update(
            stage="planning", status="in_progress", owner="work", sourceOfTruth="planning",
            currentChapter=None, currentSection=None, blockingReason=None, blockedFrom=None,
        )
        state["chapters"] = {}
        state["sections"] = {}
        atomic_write_state(self.root, state)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_section_approval_detects_global_change_and_conversion_revalidates(self) -> None:
        state = load_state(self.root)
        command_configure_sections(self.root, state, ["introduction", "method"])
        command_submit_plan(self.root, state)
        command_approve_plan(self.root, state)
        command_begin_section(self.root, state, "introduction")
        command_complete_section(self.root, state, "introduction")
        command_approve_section(self.root, state, "introduction")
        self.assertEqual(state["workflow"]["currentSection"], "method")

        command_complete_section(self.root, state, "method")
        original = (self.root / "deck/deck.preview.html").read_text(encoding="utf-8")
        (self.root / "deck/deck.preview.html").write_text(original.replace("</style>", "body{color:red}</style>"), encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "Approved section changed"):
            command_approve_section(self.root, state, "method")

        (self.root / "deck/deck.preview.html").write_text(original, encoding="utf-8")
        command_approve_section(self.root, state, "method")
        self.assertEqual(state["workflow"]["stage"], "ready_for_conversion")
        self.assertTrue(state["handoff"]["readyForCodex"])

        (self.root / "deck/assets/図 表.png").write_bytes(b"changed")
        with self.assertRaisesRegex(WorkflowError, "Approved section changed"):
            command_prepare_conversion(self.root, state)
        command_unlock_section(self.root, state, "introduction")
        self.assertEqual(state["sections"]["introduction"]["status"], "authoring")
        self.assertIsNone(state["sections"]["introduction"]["approvalDigest"])
        self.assertEqual(state["workflow"]["stage"], "html_authoring")


if __name__ == "__main__":
    unittest.main()
