"""Write a compact Bento evidence summary to the GitHub job summary."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any] | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _test_result(path: Path) -> tuple[int, bool]:
    if not path.is_file():
        return 0, False
    payload = path.read_bytes()
    text = payload.decode("utf-16") if payload.startswith((b"\xff\xfe", b"\xfe\xff")) else payload.decode("utf-8", errors="replace")
    match = re.search(r"Ran (\d+) tests?", text)
    passed = any(re.fullmatch(r"OK(?: \(skipped=\d+\))?", line.strip()) for line in text.splitlines())
    return (int(match.group(1)) if match else 0, bool(match and passed))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--determinism", required=True, type=Path)
    parser.add_argument("--tests", required=True, type=Path)
    parser.add_argument("--legacy", required=True, type=Path)
    parser.add_argument("--work-editor", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    report = _load(args.report)
    determinism = _load(args.determinism)
    work_editor = _load(args.work_editor)
    test_count, tests_passed = _test_result(args.tests)
    legacy_passed = args.legacy.is_file() and args.legacy.read_text(encoding="utf-8").strip().lstrip("\ufeff") == "PASS"
    summary = report.get("summary", {}) if report else {}
    browser = report.get("browserCheck", {}) if report else {}
    visual = report.get("visualComparison", {}) if report else {}
    failures = []
    critical_failures = []
    for pair in visual.get("pairs", []):
        if pair.get("status") != "fail":
            continue
        element_ids = [
            item.get("elementId", "unknown")
            for item in pair.get("elementComparisons", [])
            if item.get("imageComparison", {}).get("status") == "fail"
        ]
        failures.append(f"{pair['slideId']} ({', '.join(element_ids) if element_ids else 'whole slide'})")
        critical_failures.extend(
            f"{pair['slideId']}/{item.get('elementId', 'unknown')}"
            for item in pair.get("elementComparisons", [])
            if item.get("critical") and item.get("imageComparison", {}).get("status") == "fail"
        )
    unresolved = [f"{item.get('slideId')}: {item.get('elements', item.get('elementId', 'unknown'))}" for item in (report.get("diagnostics", []) if report else [])]
    resource_scan = report.get("resourceScan", {}) if report else {}
    unresolved_resources = resource_scan.get("unresolved", [])
    checks = {
        "Legacy byte match": legacy_passed,
        "Runtime integrity": bool(report and report.get("runtimeIntegrity")),
        "Serialize round-trip": bool(browser.get("serialize_roundtrip")),
        "Visual comparison": bool(visual.get("passed")),
        "Deterministic double build": bool(determinism and determinism.get("passed")),
        "Test suite": tests_passed,
        "No unresolved diagnostics": not unresolved,
        "No unresolved local resources": bool(resource_scan.get("passed")),
        "No critical crop failures": int(summary.get("criticalElementFail", 0)) == 0,
        "Work editor save": bool(work_editor and work_editor.get("workEditorSaveTest")),
        "Revision conflict rejection": bool(work_editor and work_editor.get("revisionConflictTest")),
        "Work editor runtime integrity": bool(work_editor and work_editor.get("runtimeIntegrity")),
    }
    hashes = determinism.get("sha256", {}) if determinism else {}
    native_total = sum(int(summary.get(field, 0)) for field in ("nativeText", "nativeShape", "nativeTable", "nativeChart", "nativeImage", "nativeSvg", "media"))
    lines = [
        "## HTML-first Bento evidence",
        "",
        "| Check | Result |",
        "|---|---|",
        *[f"| {name} | {'PASS' if passed else 'FAIL'} |" for name, passed in checks.items()],
        "",
        f"- Tests: {test_count}",
        f"- Slides: {summary.get('slides', 0)}",
        f"- Native elements: {native_total}",
        f"- Fallbacks: SVG {summary.get('partialSvgFallback', 0)}, image {summary.get('imageFallback', 0)}, full-slide SVG {summary.get('fullSlideSvg', 0)}",
        f"- Visual slides: pass {summary.get('visualPassSlides', 0)}, warning {summary.get('visualWarningSlides', 0)}, fail {summary.get('visualFailSlides', 0)}",
        f"- Critical crops: pass {summary.get('criticalElementPass', 0)}, warning {summary.get('criticalElementWarning', 0)}, fail {summary.get('criticalElementFail', 0)}",
        f"- Local resources: embedded {summary.get('embeddedLocalAssets', 0)}, unresolved {summary.get('unresolvedLocalResourceReferences', len(unresolved_resources))}",
        f"- Media poster embeddings: {summary.get('mediaPosterEmbeddings', 0)}",
        f"- SVG fragments preserved: {summary.get('svgFragmentPreservations', 0)}",
        f"- Recursive resource fields scanned: {summary.get('recursiveResourceScanCount', 0)}",
        f"- Visual difference: max {summary.get('maxVisualDifference', 'n/a')}, average {summary.get('averageVisualDifference', 'n/a')}",
        f"- HTML SHA-256: {(hashes.get('html') or ['unavailable'])[0]}",
        f"- Bento JSON SHA-256: {(hashes.get('bentoJson') or ['unavailable'])[0]}",
        f"- Visual failures: {', '.join(failures) if failures else 'none'}",
        f"- Critical crop failures: {', '.join(critical_failures) if critical_failures else 'none'}",
        f"- Unresolved diagnostics: {'; '.join(unresolved) if unresolved else 'none'}",
        "",
        "Artifact: `html-first-evidence`",
        "",
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("a", encoding="utf-8") as output:
        output.write("\n".join(lines))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
