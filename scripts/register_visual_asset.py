"""Register generated/derived images or extract source-original PDF figures."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bento_converter.errors import BentoConverterError
from bento_converter.visual_assets import (
    extract_pdf_figure,
    parse_source_reference,
    register_visual_asset,
)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--root", default=".")
    root.add_argument("--registry", default="deck/deck.registry.json")
    commands = root.add_subparsers(dest="command", required=True)
    register = commands.add_parser("register")
    register.add_argument("--input", required=True)
    register.add_argument("--asset-id", required=True)
    register.add_argument("--figure-id")
    register.add_argument("--kind", choices=("source-original", "source-derived", "generated"), required=True)
    register.add_argument("--role", required=True)
    register.add_argument("--source-ref", action="append", default=[])
    register.add_argument("--caption")
    register.add_argument("--description")
    register.add_argument("--generator")
    register.add_argument("--prompt-digest")
    register.add_argument("--replace", action="store_true")
    extract = commands.add_parser("extract-pdf")
    extract.add_argument("--source-id", required=True)
    extract.add_argument("--page", type=int, required=True)
    extract.add_argument("--crop", type=float, nargs=4, metavar=("X0", "Y0", "X1", "Y1"), required=True)
    extract.add_argument("--dpi", type=float, default=144)
    extract.add_argument("--asset-id", required=True)
    extract.add_argument("--locator", required=True)
    extract.add_argument("--figure-number")
    extract.add_argument("--caption")
    extract.add_argument("--role", default="source-figure")
    extract.add_argument("--replace", action="store_true")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "register":
            generator = None
            if args.generator or args.prompt_digest:
                generator = {
                    **({"name": args.generator} if args.generator else {}),
                    **({"promptDigest": args.prompt_digest} if args.prompt_digest else {}),
                }
            result = register_visual_asset(
                repository=Path(args.root), registry_path=args.registry, input_path=args.input,
                asset_id=args.asset_id, figure_id=args.figure_id, kind=args.kind, role=args.role,
                source_references=[parse_source_reference(value) for value in args.source_ref],
                caption=args.caption, description=args.description, generator=generator, replace=args.replace,
            )
        else:
            result = extract_pdf_figure(
                repository=Path(args.root), registry_path=args.registry, source_id=args.source_id,
                page=args.page, crop=tuple(args.crop), dpi=args.dpi, asset_id=args.asset_id,
                locator=args.locator, figure_number=args.figure_number, caption=args.caption,
                role=args.role, replace=args.replace,
            )
    except (BentoConverterError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
