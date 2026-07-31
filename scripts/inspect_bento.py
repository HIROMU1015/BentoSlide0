"""Inspect the native document stored inside a Bento Slides HTML file."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bento_converter.bento_validator import unknown_fields
from bento_converter.errors import BentoConverterError
from bento_converter.html_document import extract_bento_doc, load_html, locate_bento_doc


def _equations(document: dict) -> list[dict[str, object]]:
    found = []
    for slide in document.get("slides", []):
        for element in slide.get("elements", []):
            source = element.get("html")
            if element.get("type") == "text" and isinstance(source, str) and source.startswith("$$") and source.endswith("$$"):
                found.append(
                    {
                        "slideId": slide.get("id"),
                        "elementId": element.get("id"),
                        "html": source,
                        "equationId": element.get("equationId"),
                        "latexSource": element.get("latexSource"),
                    }
                )
    return found


def inspect(path: Path) -> dict[str, object]:
    html = load_html(path)
    span = locate_bento_doc(html)
    document = extract_bento_doc(html)
    slides = []
    for slide in document.get("slides", []):
        slides.append(
            {
                "id": slide.get("id"),
                "elements": [
                    {"id": element.get("id"), "type": element.get("type")}
                    for element in slide.get("elements", [])
                ],
            }
        )
    return {
        "path": str(path),
        "bentoDoc": {
            "openStart": span.open_start,
            "contentStart": span.content_start,
            "contentEnd": span.content_end,
            "closeEnd": span.close_end,
        },
        "format": document.get("format"),
        "version": document.get("version"),
        "title": document.get("title"),
        "canvas": document.get("size"),
        "slideCount": len(document.get("slides", [])),
        "slides": slides,
        "equations": _equations(document),
        "unknownFields": unknown_fields(document),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args(argv)
    try:
        result = inspect(args.file)
    except (BentoConverterError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(f"File: {result['path']}")
    print(f"bento-doc: {result['bentoDoc']}")
    print(f"Format: {result['format']}")
    print(f"Version: {result['version']}")
    print(f"Title: {result['title']}")
    print(f"Canvas: {result['canvas']}")
    print(f"Slides: {result['slideCount']}")
    for slide in result["slides"]:
        print(f"  {slide['id']}")
        for element in slide["elements"]:
            print(f"    {element['id']}: {element['type']}")
    print("Equations:")
    for equation in result["equations"]:
        print(
            f"  {equation['slideId']}/{equation['elementId']}: "
            f"{equation['html']} equationId={equation['equationId']!r} "
            f"latexSource={equation['latexSource']!r}"
        )
    print("Unknown/custom fields:")
    if not result["unknownFields"]:
        print("  none")
    for location, fields in result["unknownFields"].items():
        print(f"  {location}: {', '.join(fields)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

