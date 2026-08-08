"""Run one explicitly classified unittest group used by GitHub Actions."""

from __future__ import annotations

import argparse
import os
import unittest
from collections.abc import Iterator
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

from tests.ci_test_groups import VALID_GROUPS, classify_test_id


ROOT = Path(__file__).resolve().parents[1]


def _tests(suite: unittest.TestSuite) -> Iterator[unittest.TestCase]:
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _tests(item)
        else:
            yield item


def selected_suite(group: str) -> unittest.TestSuite:
    if group in {"browser", "determinism"}:
        os.environ["BENTO_BROWSER_TEST"] = "1"
    else:
        os.environ.pop("BENTO_BROWSER_TEST", None)
    discovered = unittest.defaultTestLoader.discover(
        str(ROOT / "tests"), pattern="test_*.py", top_level_dir=str(ROOT),
    )
    selected = unittest.TestSuite()
    for test in _tests(discovered):
        if classify_test_id(test.id()) == group:
            selected.addTest(test)
    if selected.countTestCases() == 0:
        raise RuntimeError(f"CI test group is empty: {group}")
    return selected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("group", choices=VALID_GROUPS)
    parser.add_argument("--verbosity", type=int, default=2)
    args = parser.parse_args(argv)
    browser_guard = nullcontext()
    if args.group == "unit":
        from bento_converter.browser_harness import BrowserHarness

        def reject_browser(_: BrowserHarness) -> None:
            raise AssertionError(
                "The unit CI group attempted to start Chromium; classify this test as browser or determinism"
            )

        browser_guard = mock.patch.object(BrowserHarness, "__enter__", reject_browser)
    with browser_guard:
        result = unittest.TextTestRunner(verbosity=args.verbosity).run(selected_suite(args.group))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
