"""Apply semantic Bento edits from a slide number without hand-authoring element operations."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bento_converter.artifact_transaction import write_json_atomic
from bento_converter.browser_edit import (
    REPORT_FORMAT,
    BrowserEditPostSaveError,
    validate_edit_report_path,
)
from bento_converter.errors import BentoConverterError
from bento_converter.quick_edit import (
    QUICK_PLAN_FORMAT,
    QUICK_REPORT_FORMAT,
    load_quick_edit_plan,
    run_quick_edit,
    validate_quick_edit_plan,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("plan", nargs="?", type=Path, help=f"JSON plan with format {QUICK_PLAN_FORMAT}")
    result.add_argument("--editor-url", required=True, help="Running loopback Work editor URL")
    result.add_argument("--slide", help="1-based slide number or exact Bento slide ID")
    result.add_argument("--target", default="body", help="Semantic target group; defaults to body")
    result.add_argument("--move-x", type=float, default=0)
    result.add_argument("--move-y", type=float, default=0)
    result.add_argument("--text-color")
    result.add_argument("--shape-fill")
    result.add_argument("--shape-stroke")
    result.add_argument("--align", choices=["left", "center-x", "right", "top", "center-y", "bottom"])
    result.add_argument("--scale-x", type=float)
    result.add_argument("--scale-y", type=float)
    result.add_argument("--fit-margin", type=float)
    result.add_argument("--allow-fit-scale", action="store_true")
    result.add_argument("--dry-run", action="store_true")
    result.add_argument("--screenshots-dir", type=Path)
    result.add_argument("--report", type=Path)
    result.add_argument("--browser-executable", type=Path)
    return result


def _direct_plan(args: argparse.Namespace) -> dict:
    if args.plan is not None:
        return load_quick_edit_plan(args.plan)
    if not args.slide:
        raise BentoConverterError("Either a quick edit plan or --slide is required")
    try:
        slide: str | int = int(args.slide)
    except ValueError:
        slide = args.slide
    operations: list[dict] = []
    if args.move_x or args.move_y:
        operations.append({"op": "shift", "target": args.target, "dx": args.move_x, "dy": args.move_y})
    if args.text_color:
        operations.append({"op": "text-style", "target": args.target, "properties": {"color": args.text_color}})
    shape_properties = {key: value for key, value in {
        "fill": args.shape_fill, "stroke": args.shape_stroke,
    }.items() if value is not None}
    if shape_properties:
        operations.append({"op": "shape-style", "target": args.target, "properties": shape_properties})
    if args.align:
        operations.append({"op": "align", "target": args.target, "alignment": args.align})
    if args.scale_x is not None or args.scale_y is not None:
        operations.append({
            "op": "resize-group", "target": args.target,
            "scaleX": args.scale_x if args.scale_x is not None else 1,
            "scaleY": args.scale_y if args.scale_y is not None else 1,
        })
    if args.fit_margin is not None:
        operations.append({
            "op": "fit-within-slide", "target": args.target,
            "margin": args.fit_margin, "allowScale": args.allow_fit_scale,
        })
    if not operations:
        raise BentoConverterError("Direct quick edit arguments did not request any change")
    return validate_quick_edit_plan({"format": QUICK_PLAN_FORMAT, "slide": slide, "operations": operations})


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        plan = _direct_plan(args)
        report_path = (
            validate_edit_report_path(
                args.editor_url, args.report,
                input_paths=(() if args.plan is None else (args.plan,)),
                allowed_formats=(REPORT_FORMAT, QUICK_REPORT_FORMAT),
            )
            if args.report is not None else None
        )
        report = run_quick_edit(
            args.editor_url, plan, dry_run=args.dry_run,
            screenshots_dir=args.screenshots_dir, browser_executable=args.browser_executable,
        )
    except BrowserEditPostSaveError as exc:
        report = exc.report
        exit_code = 3
    except (BentoConverterError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    else:
        exit_code = 0
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if report_path is not None:
        write_json_atomic(report_path, report)
    print(payload, end="")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
