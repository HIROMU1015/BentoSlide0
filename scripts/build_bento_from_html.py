"""Build a deterministic editable Bento deck from chapter HTML and registry JSON."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from bento_converter.errors import BentoConverterError
from bento_converter.html_pipeline import build_from_html


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--html", type=Path, help="Single deck HTML source")
    result.add_argument("--registry", type=Path, help="Registry paired with --html")
    result.add_argument("--html-dir", type=Path, help="Directory containing sorted chapter HTML files")
    result.add_argument("--registry-dir", type=Path, help="Directory containing matching *.registry.json files")
    result.add_argument("--base", required=True, type=Path, help="Official Bento base .bento.html")
    result.add_argument("--output", required=True, type=Path, help="Output presentation.bento.html")
    result.add_argument("--browser-executable", type=Path, help="Optional Chrome/Edge executable")
    result.add_argument("--skip-bento-browser-check", action="store_true", help="Skip final Bento UI/round-trip screenshots (computed layout still requires Chromium)")
    return result


def run(args: argparse.Namespace) -> int:
    explicit = args.html is not None or args.registry is not None
    modular = args.html_dir is not None or args.registry_dir is not None
    if explicit == modular:
        raise BentoConverterError("Choose exactly one source form: --html/--registry or --html-dir/--registry-dir.")
    if explicit and (args.html is None or args.registry is None):
        raise BentoConverterError("Single-file conversion requires both --html and --registry.")
    if modular and (args.html_dir is None or args.registry_dir is None):
        raise BentoConverterError("Modular conversion requires both --html-dir and --registry-dir.")
    result = build_from_html(
        html_dir=args.html_dir,
        registry_dir=args.registry_dir,
        html_path=args.html,
        registry_path=args.registry,
        base_path=args.base,
        output_path=args.output,
        browser_executable=args.browser_executable,
        browser_check=not args.skip_bento_browser_check,
    )
    print(f"Built: {result.html_path}")
    print(f"Native JSON: {result.json_path}")
    print(f"Report: {result.report_path}")
    print(f"Slides: {len(result.document['slides'])}")
    print(f"Source screenshots: {len(result.source_screenshots)}")
    print(f"Bento screenshots: {len(result.bento_screenshots)}")
    print("Runtime integrity: PASS")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run(parser().parse_args(argv))
    except (BentoConverterError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
