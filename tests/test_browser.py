from __future__ import annotations

import os
import unittest
from pathlib import Path

from bento_converter.browser_check import run_browser_check

ROOT = Path(__file__).parent.parent


@unittest.skipUnless(
    os.environ.get("BENTO_BROWSER_TEST") == "1",
    "Set BENTO_BROWSER_TEST=1 to run the local Chrome/Playwright integration test.",
)
class BrowserIntegrationTests(unittest.TestCase):
    def test_demo_render_edit_serialize_and_metadata(self):
        report = run_browser_check(
            ROOT / "demo.generated.bento.html",
            design_path=ROOT / "gpt_bento_design.json",
        )
        self.assertEqual(report.slide_count, 2)
        self.assertTrue(report.ui_selection)
        self.assertTrue(report.text_edit)
        self.assertTrue(report.shape_move)
        self.assertTrue(report.equation_rerender)
        self.assertTrue(report.equation_id_preserved)
        self.assertTrue(report.latex_source_preserved)
        self.assertFalse(report.latex_source_auto_synced)


if __name__ == "__main__":
    unittest.main()
