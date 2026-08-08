"""Validate optional planning/visual-plan.yaml visual-strategy metadata."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bento_converter.errors import BentoConverterError
from bento_converter.visual_planning import load_visual_plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default="planning/visual-plan.yaml")
    args = parser.parse_args(argv)
    try:
        plan = load_visual_plan(Path(args.path))
    except BentoConverterError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"path": args.path, "slides": len(plan["slides"]), "valid": True}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
