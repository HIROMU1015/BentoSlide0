"""Apply a reusable Bento edit plan through the fastest safe Work editor route."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bento_converter.browser_edit import (
    CAPABILITIES,
    PLAN_FORMAT,
    BrowserEditPostSaveError,
    load_browser_edit_plan,
    run_browser_edit,
    validate_edit_report_path,
)
from bento_converter.artifact_transaction import write_json_atomic
from bento_converter.errors import BentoConverterError


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("plan", nargs="?", type=Path, help=f"JSON plan with format {PLAN_FORMAT}")
    result.add_argument("--editor-url", help="Running loopback Work editor URL")
    result.add_argument("--screenshots-dir", type=Path, help="Capture only changed slides")
    result.add_argument("--report", type=Path, help="Write the scoped edit report")
    result.add_argument("--browser-executable", type=Path, help="Chrome/Edge executable override")
    result.add_argument("--list-capabilities", action="store_true", help="Print supported reusable edit operations")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.list_capabilities:
        print(json.dumps({"format": PLAN_FORMAT, "capabilities": CAPABILITIES}, ensure_ascii=False, indent=2))
        return 0
    if args.plan is None or not args.editor_url:
        parser().error("plan and --editor-url are required unless --list-capabilities is used")
    try:
        report_path = (
            validate_edit_report_path(
                args.editor_url, args.report, input_paths=(args.plan,),
            )
            if args.report is not None else None
        )
        report = run_browser_edit(
            args.editor_url,
            load_browser_edit_plan(args.plan),
            screenshots_dir=args.screenshots_dir,
            browser_executable=args.browser_executable,
        )
    except BrowserEditPostSaveError as exc:
        payload = json.dumps(exc.report, ensure_ascii=False, indent=2) + "\n"
        if report_path is not None:
            write_json_atomic(report_path, exc.report)
        print(payload, end="")
        print(f"SAVED_BUT_POST_SAVE_CHECK_FAILED: {exc}", file=sys.stderr)
        return 3
    except (BentoConverterError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if report_path is not None:
        write_json_atomic(report_path, report)
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
