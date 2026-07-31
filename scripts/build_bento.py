"""Build an editable .bento.html from a GPT design JSON file."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from bento_converter.bento_validator import validate_bento_doc, validate_conversion
from bento_converter.converter import convert_design
from bento_converter.design_loader import load_design
from bento_converter.errors import BentoConverterError
from bento_converter.html_document import (
    assert_runtime_integrity,
    extract_bento_doc,
    load_html,
    write_embedded_document,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--base", required=True, type=Path, help="Official Bento base HTML")
    result.add_argument("--design", required=True, type=Path, help="GPT design JSON")
    result.add_argument("--output", required=True, type=Path, help="Generated .bento.html")
    result.add_argument("--doc-id", help="Override document.docId with a UUID")
    result.add_argument(
        "--modified",
        help="Override document.modified with ISO-8601 or 'now'. Required if absent from design.",
    )
    return result


def run(args: argparse.Namespace) -> int:
    design = load_design(args.design)
    result = convert_design(design, doc_id=args.doc_id, modified=args.modified)
    validate_bento_doc(result.document)
    validate_conversion(design, result.document)

    write_embedded_document(args.base, args.output, result.document)
    base_html = load_html(args.base)
    output_html = load_html(args.output)
    roundtrip = extract_bento_doc(output_html)
    if roundtrip != result.document:
        raise BentoConverterError("Post-write round-trip mismatch in generated #bento-doc JSON.")
    assert_runtime_integrity(base_html, output_html)

    print(f"Built: {args.output}")
    print(f"Document: {result.document['title']}")
    print(f"docId: {result.document['docId']}")
    print(f"modified: {result.document['modified']}")
    print(f"Slides: {len(result.document['slides'])}")
    print("Runtime integrity: PASS")
    for warning in result.warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return run(args)
    except (BentoConverterError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

