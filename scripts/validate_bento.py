"""Validate a Bento Slides HTML document and optionally its runtime integrity."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from bento_converter.bento_validator import validate_bento_html
from bento_converter.errors import BentoConverterError
from bento_converter.html_document import load_html, runtime_fingerprint


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument("--base", type=Path, help="Compare runtime outside #bento-doc")
    args = parser.parse_args(argv)
    try:
        html = load_html(args.file)
        base_html = load_html(args.base) if args.base else None
        document, report = validate_bento_html(html, base_html=base_html)
    except (BentoConverterError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print("Bento JSON: PASS")
    print(f"Format: {document['format']} v{document['version']}")
    print(f"Slides: {len(document['slides'])}")
    print(f"Runtime SHA-256: {runtime_fingerprint(html)}")
    if base_html is not None:
        print("Runtime integrity: PASS")
        print("Only bento-doc differs from base file.")
    for warning in report.warnings:
        print(f"WARNING: {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

