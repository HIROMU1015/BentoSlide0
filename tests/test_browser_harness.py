from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from bento_converter.browser_harness import BrowserHarness, canonical_digest
from bento_converter.incremental_cache import IncrementalSlideCache


class BrowserHarnessUnitTests(unittest.TestCase):
    def profile(self, width: int) -> dict:
        return {
            "viewport": {"width": width, "height": 900},
            "deviceScaleFactor": 1,
            "locale": "en-US",
            "timezoneId": "UTC",
            "observations": 1,
            "fontFaces": [{"family": "Arial", "status": "loaded"}],
            "computedFamilies": ["Arial"],
            "usedPlatformFonts": [{"family": "Arial", "custom": False, "glyphCount": 12}],
            "platformFontProbeTruncated": False,
        }

    def harness(self) -> BrowserHarness:
        harness = BrowserHarness()
        harness.browser = SimpleNamespace(version="149.0.7827.55")
        harness._profiles = {
            "sourceLayout": self.profile(1400),
            "bentoCheck": self.profile(1600),
        }
        return harness

    def test_environment_report_is_versioned_privacy_safe_and_profiled(self) -> None:
        report = self.harness().report()
        self.assertEqual(report["format"], "bento/browser-environment/v1")
        self.assertTrue(report["environmentDigest"].startswith("sha256:"))
        environment = report["browserEnvironment"]
        self.assertEqual(environment["profiles"]["sourceLayout"]["viewport"]["width"], 1400)
        self.assertEqual(environment["profiles"]["bentoCheck"]["viewport"]["width"], 1600)
        self.assertTrue(environment["fonts"]["digest"].startswith("sha256:"))
        serialized = json.dumps(report)
        self.assertNotIn("hostname", serialized.lower())
        self.assertNotIn("username", serialized.lower())
        self.assertNotIn("executablePath", serialized)

    def test_profile_cache_digest_ignores_content_glyph_count(self) -> None:
        first = self.harness()
        second = self.harness()
        second._profiles["sourceLayout"]["usedPlatformFonts"][0]["glyphCount"] = 999
        self.assertEqual(first.profile_digest("sourceLayout"), second.profile_digest("sourceLayout"))

    def test_profile_and_canonical_digests_track_real_configuration(self) -> None:
        first = self.harness()
        second = self.harness()
        second._profiles["sourceLayout"]["viewport"]["width"] = 1399
        self.assertNotEqual(first.profile_digest("sourceLayout"), second.profile_digest("sourceLayout"))
        self.assertEqual(canonical_digest({"b": 2, "a": 1}), canonical_digest({"a": 1, "b": 2}))

    def test_network_policy_allows_self_contained_and_loopback_only(self) -> None:
        self.assertTrue(BrowserHarness._request_allowed("file:///tmp/deck.html"))
        self.assertTrue(BrowserHarness._request_allowed("data:image/png;base64,AA=="))
        self.assertTrue(BrowserHarness._request_allowed("http://127.0.0.1:8765/api/status"))
        self.assertFalse(BrowserHarness._request_allowed("https://example.com/font.woff2"))

    def test_incremental_cache_preserves_nested_insertion_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "record.json"
            payload = {"option": {"xAxis": {}, "yAxis": {}, "series": []}}
            IncrementalSlideCache._atomic_json(path, payload)
            restored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(list(restored["option"]), ["xAxis", "yAxis", "series"])


if __name__ == "__main__":
    unittest.main()
