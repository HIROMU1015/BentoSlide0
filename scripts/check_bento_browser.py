"""Check rendering, editing, save round-trip, metadata, and screenshots in a browser."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bento_converter.browser_check import run_browser_check
from bento_converter.errors import BentoConverterError


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("file", type=Path, help="Generated .bento.html")
    result.add_argument("--design", type=Path, help="Compare all slides/elements with GPT design JSON")
    result.add_argument("--screenshots-dir", type=Path, help="Write one PNG per slide")
    result.add_argument("--screenshot-prefix", help="PNG prefix; defaults to <deck>-slide")
    result.add_argument("--browser-executable", type=Path, help="Chrome/Edge executable override")
    result.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        report = run_browser_check(
            args.file,
            design_path=args.design,
            screenshots_dir=args.screenshots_dir,
            screenshot_prefix=args.screenshot_prefix,
            browser_executable=args.browser_executable,
        )
    except (BentoConverterError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    result = report.as_dict()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(f"Browser: {report.browser}")
    print(
        f"Bento UI: PASS ({report.rendered_slide_count}/{report.slide_count} slides, "
        f"{report.element_count} elements)"
    )
    print(f"Coordinates: PASS ({len(report.checked_coordinates)} elements)")
    print(
        "Detected source types: "
        + ", ".join(f"{kind}={count}" for kind, count in report.detected_types.items())
    )
    print(f"UI selection only: {_status(report.ui_selection)}")
    print(f"Bento API text edit: {_status(report.api_text_edit)}")
    print(f"Bento API shape move: {_status(report.api_shape_move)}")
    print(f"Bento API equation edit: {_status(report.api_equation_edit)}")
    print(f"Bento API equation MathML rerender: {_status(report.api_equation_rerender)}")
    print(f"serialize round-trip: {_status(report.serialize_roundtrip)}")
    print(f"equationId preserved: {report.equation_id_preserved}")
    print(f"latexSource preserved: {report.latex_source_preserved}")
    print(f"latexSource auto-synced: {report.latex_source_auto_synced}")
    print(f"Metadata source of truth: {report.metadata_source_of_truth}")
    for screenshot in report.screenshots:
        print(f"Screenshot: {screenshot}")
    return 0


def _status(value: bool | None) -> str:
    if value is None:
        return "NOT APPLICABLE"
    return "PASS" if value else "FAIL"


if __name__ == "__main__":
    raise SystemExit(main())
