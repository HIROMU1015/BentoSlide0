from __future__ import annotations

import unittest

from scripts.run_ci_test_group import ROOT, _tests
from tests.ci_test_groups import (
    CLASS_GROUPS,
    TEST_GROUP_OVERRIDES,
    VALID_GROUPS,
    classify_test_id,
)


class CiTestGroupContractTests(unittest.TestCase):
    def discovered_ids(self) -> set[str]:
        suite = unittest.defaultTestLoader.discover(
            str(ROOT / "tests"), pattern="test_*.py", top_level_dir=str(ROOT),
        )
        return {test.id() for test in _tests(suite)}

    def test_every_test_class_and_override_is_classified(self) -> None:
        test_ids = self.discovered_ids()
        discovered_classes = {test_id.rpartition(".")[0] for test_id in test_ids}
        self.assertEqual(discovered_classes, set(CLASS_GROUPS))
        self.assertLessEqual(set(TEST_GROUP_OVERRIDES), test_ids)
        groups = {classify_test_id(test_id) for test_id in test_ids}
        self.assertEqual(groups, set(VALID_GROUPS))
        self.assertTrue(all(group in VALID_GROUPS for group in CLASS_GROUPS.values()))
        self.assertTrue(all(group in VALID_GROUPS for group in TEST_GROUP_OVERRIDES.values()))

    def test_ci_uses_the_classified_group_runner(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        for group in VALID_GROUPS:
            self.assertIn(f"python -m scripts.run_ci_test_group {group}", workflow)


if __name__ == "__main__":
    unittest.main()
