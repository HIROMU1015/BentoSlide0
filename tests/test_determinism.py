"""Tests for normalized evidence and full double-build determinism."""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from bento_converter.determinism import canonical_sha256, normalize_evidence
from scripts.check_html_first_determinism import run_check

ROOT = Path(__file__).resolve().parents[1]


class DeterminismHelpersTests(unittest.TestCase):
    def test_normalization_removes_browser_and_rewrites_build_root(self) -> None:
        value = {"browser": "Chromium 999", "path": "C:/tmp/run/evidence.png", "values": [2, 1]}
        self.assertEqual(normalize_evidence(value, "C:/tmp/run"), {"path": "$BUILD_ROOT/evidence.png", "values": [2, 1]})

    def test_canonical_sha_is_independent_of_dictionary_order(self) -> None:
        self.assertEqual(canonical_sha256({"b": 2, "a": 1}), canonical_sha256({"a": 1, "b": 2}))


@unittest.skipUnless(os.environ.get("BENTO_BROWSER_TEST") == "1", "Set BENTO_BROWSER_TEST=1 for Chromium determinism integration.")
class DeterminismBrowserTests(unittest.TestCase):
    def test_two_independent_build_directories_are_identical(self) -> None:
        report = run_check(
            html_dir=ROOT / "tests" / "fixtures" / "html_first",
            registry_dir=ROOT / "tests" / "fixtures" / "html_first",
            base=ROOT / "Bento_Slides.base.bento.html",
        )
        self.assertTrue(report["passed"], report)
        self.assertTrue(all(report["checks"].values()))
        self.assertTrue(all(len(values) == 2 and values[0] == values[1] for values in report["sha256"].values()))


if __name__ == "__main__":
    unittest.main()
