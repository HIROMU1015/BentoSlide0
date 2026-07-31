from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent


class DemoContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.design = json.loads(
            (ROOT / "gpt_bento_design.json").read_text(encoding="utf-8")
        )

    def test_demo_has_two_slides(self):
        self.assertEqual(len(self.design["slides"]), 2)

    def test_demo_title_copy(self):
        title = next(
            element
            for element in self.design["slides"][0]["elements"]
            if element["id"] == "slide-1-title"
        )
        self.assertEqual(title["content"], "GPTが座標まで設計する")

    def test_demo_required_element_ids(self):
        element_ids = {
            element["id"]
            for slide in self.design["slides"]
            for element in slide["elements"]
        }
        self.assertTrue(
            {"slide-1-title", "slide-1-shape", "hamiltonian-equation"}
            <= element_ids
        )


if __name__ == "__main__":
    unittest.main()
