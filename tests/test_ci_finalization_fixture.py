from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bento_converter.work_editor_storage import WorkEditorStorage
from scripts.deck_workflow import load_final_baseline, load_state, validate_output_bundle, validate_state
from tests.finalization_fixture import main


ROOT = Path(__file__).resolve().parents[1]


class CiFinalizationFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "Bento Slide 日本語 fixture"
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_fixture_uses_real_gates_and_satisfies_final_editor_contract(self) -> None:
        self.assertEqual(main([
            "--root", str(self.root), "--bento-port", "9876", "--confirm-disposable-fixture",
        ]), 0)
        state = load_state(self.root)

        self.assertEqual(state["schemaVersion"], 2)
        self.assertEqual(state["workflow"]["stage"], "bento_finalization")
        self.assertTrue(state["handoff"]["readyForFinalEditing"])
        self.assertEqual(state["approvals"]["bentoContent"]["status"], "approved")
        self.assertEqual(state["approvals"]["finalBento"]["status"], "pending")
        self.assertEqual(state["preview"]["bentoPort"], 9876)
        validate_state(self.root, state)

        bundle = validate_output_bundle(self.root, state, require_final=True)
        baseline, _ = load_final_baseline(self.root, state, bundle["generatedDocument"])
        outputs = state["outputs"]
        storage = WorkEditorStorage(
            source=self.root / outputs["authoringHtml"],
            target=self.root / outputs["finalHtml"],
            registry=self.root / outputs["finalRegistry"],
            repository=self.root,
            baseline_document=baseline,
            state_path=self.root / "deck.yaml",
        )
        status = storage.status()
        self.assertEqual(status["editingMode"], "finalization")
        self.assertEqual(status["target"], Path(outputs["finalHtml"]).name)

    def test_cli_requires_explicit_disposable_confirmation(self) -> None:
        with self.assertRaisesRegex(SystemExit, "Refusing"):
            main(["--root", str(self.root)])

    def test_windows_smoke_uses_explicit_initialized_and_whole_deck_states(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        save_state = workflow.index('Copy-Item deck.yaml $wholeDeckState')
        install_initialized = workflow.index(
            'Copy-Item tests/fixtures/deck_v2.initialized.yaml deck.yaml'
        )
        initialized_start = workflow.index('"initialized start_deck_workspace.cmd failed')
        restore_whole_deck = workflow.index('Copy-Item $wholeDeckState deck.yaml')
        whole_deck_start = workflow.index('"whole-deck start_deck_workspace.cmd failed')
        self.assertLess(
            save_state,
            install_initialized,
        )
        self.assertLess(install_initialized, initialized_start)
        self.assertLess(initialized_start, restore_whole_deck)
        self.assertLess(restore_whole_deck, whole_deck_start)
        self.assertIn(
            '$workspaceStatus.stage -ne "html_review" -or $workspaceStatus.mode -ne "single"',
            workflow,
        )


if __name__ == "__main__":
    unittest.main()
