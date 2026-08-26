"""Check Bento HTML annotation structure without launching Chromium."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from bento_converter.errors import BentoConverterError
from bento_converter.html_preflight import analyze_html_sources, write_preflight_report
from bento_converter.html_source import discover_chapters, discover_source_unit, merge_registries


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--html", type=Path, help="Single deck HTML source")
    result.add_argument("--registry", type=Path, help="Registry paired with --html")
    result.add_argument("--html-dir", type=Path, help="Directory containing chapter HTML files")
    result.add_argument("--registry-dir", type=Path, help="Directory containing chapter registries")
    result.add_argument("--report", type=Path, help="Optional deterministic JSON report path")
    result.add_argument(
        "--strict-editability", action="store_true",
        help="Return a non-zero status for editability warnings as well as safety errors",
    )
    result.add_argument("--verbose", action="store_true", help="Print every warning; the JSON report always contains all issues")
    return result


def run(args: argparse.Namespace) -> int:
    explicit = args.html is not None or args.registry is not None
    modular = args.html_dir is not None or args.registry_dir is not None
    if explicit == modular:
        raise BentoConverterError("Choose exactly one source form: --html/--registry or --html-dir/--registry-dir.")
    if explicit:
        if args.html is None or args.registry is None:
            raise BentoConverterError("Single-file preflight requires both --html and --registry.")
        units = [discover_source_unit(args.html, args.registry)]
    else:
        if args.html_dir is None or args.registry_dir is None:
            raise BentoConverterError("Modular preflight requires both --html-dir and --registry-dir.")
        units = discover_chapters(args.html_dir, args.registry_dir)
    report = analyze_html_sources(units, merge_registries(units))
    if args.report:
        write_preflight_report(args.report, report)
    summary = report.as_dict()["summary"]
    print(
        f"HTML preflight: {'PASS' if report.passed else 'FAIL'}; "
        f"slides={summary['slides']}; annotated={summary['annotatedElements']}; "
        f"errors={summary['errors']}; warnings={summary['warnings']}"
    )
    errors = list(report.errors)
    warnings = list(report.warnings)
    visible = errors + (warnings if getattr(args, "verbose", False) else warnings[:20])
    for item in visible:
        location = f"{item.slide_id or '<document>'}/{item.element_id or '<none>'}"
        print(f"{item.severity.upper()}: {item.code}: {location}: {item.message} Fix: {item.fix}")
    hidden = len(report.issues) - len(visible)
    if hidden:
        print(f"... {hidden} additional warnings are recorded in the JSON report; use --verbose to print all.")
    if not report.passed:
        return 2
    return 3 if args.strict_editability and report.warnings else 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run(parser().parse_args(argv))
    except (BentoConverterError, OSError, UnicodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
