"""Run an isolated, resumable HTML-to-Bento converter repair check."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from bento_converter.conversion_repair import (
    DIAGNOSTICS_FORMAT,
    STATUS_FORMAT,
    FocusedTest,
    RepairDiagnostic,
    atomic_json,
    create_full_deck_reproducer,
    create_single_slide_reproducer,
    diagnostics_from_exception,
    preserve_failure_evidence,
    select_focused_tests,
)
from bento_converter.artifact_transaction import WriterLease
from bento_converter.errors import BentoConverterError
from bento_converter.html_pipeline import HtmlBuildResult, build_from_html
from scripts.deck_workflow import _repo_path, load_state


BuildFunction = Callable[..., HtmlBuildResult]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_workspace(root: Path, supplied: Path | None) -> Path:
    workspace = (supplied if supplied and supplied.is_absolute() else root / (supplied or "output/conversion-repair")).resolve()
    try:
        workspace.relative_to(root)
    except ValueError as exc:
        raise BentoConverterError(f"Conversion repair workspace escapes the repository: {workspace}") from exc
    return workspace


def _arguments(root: Path, state: dict[str, Any], workspace: Path, browser_executable: Path | None) -> dict[str, Any]:
    authoring = state["authoring"]
    values: dict[str, Any] = {
        "base_path": root / "Bento_Slides.base.bento.html",
        "output_path": workspace / "current" / "presentation.repair.bento.html",
        "browser_executable": browser_executable,
        "browser_check": True,
        "incremental": True,
    }
    if authoring["mode"] == "modular":
        values.update(html_dir=root / "chapters", registry_dir=root / "chapters")
    else:
        values.update(
            html_path=_repo_path(root, authoring["entryHtml"], field="authoring.entryHtml"),
            registry_path=_repo_path(root, authoring["registry"], field="authoring.registry"),
        )
    return values


class ProgressRecorder:
    def __init__(self, path: Path, *, attempt_id: str, changed_paths: list[str]) -> None:
        self.path = path
        self.attempt_id = attempt_id
        self.changed_paths = changed_paths
        self.started_at = _utc_now()
        self.started = time.perf_counter()
        self.phase_started = self.started
        self.phase: str | None = None
        self.timings: dict[str, float] = {}
        self.details: dict[str, Any] = {}

    def _close_phase(self) -> None:
        if self.phase is not None:
            elapsed = time.perf_counter() - self.phase_started
            self.timings[self.phase] = round(self.timings.get(self.phase, 0.0) + elapsed, 4)

    def __call__(self, phase: str, details: dict[str, Any]) -> None:
        self._close_phase()
        self.phase = phase
        self.phase_started = time.perf_counter()
        self.details = details
        self.write("running")

    def finish(self) -> None:
        self._close_phase()
        self.phase_started = time.perf_counter()

    def payload(self, status: str, **extra: Any) -> dict[str, Any]:
        return {
            "format": STATUS_FORMAT,
            "status": status,
            "attemptId": self.attempt_id,
            "phase": self.phase,
            "startedAt": self.started_at,
            "updatedAt": _utc_now(),
            "elapsedSeconds": round(time.perf_counter() - self.started, 4),
            "phaseTimings": self.timings,
            "phaseDetails": self.details,
            "changedPaths": self.changed_paths,
            **extra,
        }

    def write(self, status: str, **extra: Any) -> dict[str, Any]:
        value = self.payload(status, **extra)
        atomic_json(self.path, value)
        return value


def _load_previous_status(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) and value.get("format") == STATUS_FORMAT else None


def _previous_diagnostics(value: dict[str, Any] | None) -> list[RepairDiagnostic]:
    if not value:
        return []
    result: list[RepairDiagnostic] = []
    for item in value.get("diagnostics", []):
        if not isinstance(item, dict):
            continue
        result.append(RepairDiagnostic(
            code=str(item.get("code") or "previous-failure"),
            phase=str(item.get("phase") or value.get("phase") or "converting"),
            message=str(item.get("message") or "Previous repair failure"),
            slide_id=item.get("slideId"), element_id=item.get("elementId"),
            field=item.get("field"), actual=item.get("actual"), fix=item.get("fix"),
            repairable=bool(item.get("repairable", True)),
        ))
    return result


def _run_focused_tests(root: Path, tests: list[FocusedTest], report_path: Path) -> tuple[bool, list[dict[str, Any]]]:
    results: list[dict[str, Any]] = []
    for browser in (False, True):
        selected = [test.test_id for test in tests if test.browser == browser]
        if not selected:
            continue
        command = [sys.executable, "-m", "unittest", *selected]
        environment = os.environ.copy()
        if browser:
            environment["BENTO_BROWSER_TEST"] = "1"
        started = time.perf_counter()
        completed = subprocess.run(
            command, cwd=root, env=environment, capture_output=True, text=True, encoding="utf-8",
            errors="replace", check=False,
        )
        results.append({
            "browser": browser,
            "tests": selected,
            "command": command,
            "returnCode": completed.returncode,
            "elapsedSeconds": round(time.perf_counter() - started, 4),
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        })
    atomic_json(report_path, {"format": "bento/conversion-repair-tests/v1", "results": results})
    return all(item["returnCode"] == 0 for item in results), results


def _capture_failure(
    *, root: Path, workspace: Path, current_root: Path, attempt_id: str,
    build_arguments: dict[str, Any], diagnostics: list[RepairDiagnostic], tests: list[FocusedTest],
) -> dict[str, Any]:
    attempt_root = workspace / "attempts" / attempt_id
    preserved = preserve_failure_evidence(current_root, attempt_root)
    reproducers: list[dict[str, Any]] = []
    warnings: list[str] = []
    html = build_arguments.get("html_path")
    registry = build_arguments.get("registry_path")
    if html is not None and registry is not None:
        slide_ids = sorted({item.slide_id for item in diagnostics if item.slide_id})
        try:
            if slide_ids:
                for slide_id in slide_ids:
                    safe_id = re.sub(r"[^A-Za-z0-9._-]+", "-", slide_id).strip("-.")[:48]
                    safe_id = safe_id or "slide"
                    safe_id += "-" + hashlib.sha256(slide_id.encode("utf-8")).hexdigest()[:8]
                    destination = attempt_root / "reproducers" / safe_id
                    reproducers.append(create_single_slide_reproducer(
                        repository=root, html_path=html, registry_path=registry,
                        base_path=build_arguments["base_path"], slide_id=slide_id,
                        destination=destination,
                    ))
            else:
                reproducers.append(create_full_deck_reproducer(
                    repository=root, html_path=html, registry_path=registry,
                    base_path=build_arguments["base_path"],
                    destination=attempt_root / "reproducer",
                ))
        except (BentoConverterError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            warnings.append(str(exc))
            try:
                reproducers.append(create_full_deck_reproducer(
                    repository=root, html_path=html, registry_path=registry,
                    base_path=build_arguments["base_path"],
                    destination=attempt_root / "reproducer",
                ))
            except (BentoConverterError, OSError, UnicodeDecodeError, json.JSONDecodeError) as fallback_exc:
                warnings.append(str(fallback_exc))
    else:
        warnings.append("Modular failures preserve evidence but do not yet create per-slide reproducers.")
    report = {
        "format": DIAGNOSTICS_FORMAT,
        "attemptId": attempt_id,
        "diagnostics": [item.as_dict() for item in diagnostics],
        "focusedTests": [item.as_dict() for item in tests],
        "preservedEvidence": preserved,
        "reproducers": reproducers,
        "warnings": warnings,
    }
    atomic_json(attempt_root / "diagnostics.json", report)
    return {
        "diagnosticsPath": (attempt_root / "diagnostics.json").relative_to(root).as_posix(),
        "reproducerPaths": [
            str(path.relative_to(root).as_posix())
            for path in sorted((attempt_root / "reproducers").glob("*/reproducer.json"))
        ] + ([str((attempt_root / "reproducer/reproducer.json").relative_to(root).as_posix())]
             if (attempt_root / "reproducer/reproducer.json").is_file() else []),
        "snapshotWarnings": warnings,
    }


def _run_repair_owned(args: argparse.Namespace, *, build: BuildFunction = build_from_html) -> int:
    root = args.root.resolve()
    state = load_state(root)
    workspace = _safe_workspace(root, args.workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    status_path = workspace / "status.json"
    previous = _load_previous_status(status_path)
    attempt_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    changed_paths = [str(value).replace("\\", "/") for value in args.changed_path]
    recorder = ProgressRecorder(status_path, attempt_id=attempt_id, changed_paths=changed_paths)
    build_arguments = _arguments(root, state, workspace, args.browser_executable)
    build_arguments["progress"] = recorder
    try:
        result = build(**build_arguments)
    except Exception as exc:
        recorder.finish()
        phase = recorder.phase or "discovering"
        diagnostics = diagnostics_from_exception(exc, phase=phase)
        tests = select_focused_tests(diagnostics, changed_paths=changed_paths)
        failure = _capture_failure(
            root=root, workspace=workspace, current_root=workspace / "current",
            attempt_id=attempt_id, build_arguments=build_arguments,
            diagnostics=diagnostics, tests=tests,
        )
        recorder.write(
            "failed", error=str(exc), retryable=True,
            diagnostics=[item.as_dict() for item in diagnostics],
            focusedTests=[item.as_dict() for item in tests], **failure,
        )
        print(f"ERROR: {exc}", file=sys.stderr)
        print(f"Repair diagnostics: {root / failure['diagnosticsPath']}", file=sys.stderr)
        return 2

    recorder.finish()
    previous_diagnostics = _previous_diagnostics(previous)
    tests = select_focused_tests(previous_diagnostics, changed_paths=changed_paths)
    test_payload: dict[str, Any] = {"focusedTests": [item.as_dict() for item in tests]}
    if args.run_focused_tests:
        passed, results = _run_focused_tests(root, tests, workspace / "focused-tests.json")
        test_payload.update(
            focusedTestsPassed=passed,
            focusedTestsReport=(workspace / "focused-tests.json").relative_to(root).as_posix(),
            focusedTestRuns=[{
                key: value for key, value in item.items() if key not in {"stdout", "stderr"}
            } for item in results],
        )
        if not passed:
            recorder.write("tests-failed", retryable=True, **test_payload)
            print(f"Focused repair tests failed: {workspace / 'focused-tests.json'}", file=sys.stderr)
            return 3
    recorder.write(
        "succeeded", retryable=False,
        output=result.html_path.relative_to(root).as_posix(),
        report=result.report_path.relative_to(root).as_posix(),
        incrementalCache=result.report.get("incrementalCache", {}),
        formalArtifactsChanged=False,
        **test_payload,
    )
    print(f"Repair build: PASS ({result.html_path})")
    print(f"Repair status: {status_path}")
    if not args.run_focused_tests:
        print("Focused tests are listed in status.json; run again with --run-focused-tests after a code fix.")
    return 0


def run_repair(args: argparse.Namespace, *, build: BuildFunction = build_from_html) -> int:
    root = args.root.resolve()
    workspace = _safe_workspace(root, args.workspace)
    lease = WriterLease(
        root,
        (workspace / "status.json", workspace / "current/presentation.repair.bento.html"),
        state_root=workspace / ".transactions",
    )
    lease.acquire()
    try:
        return _run_repair_owned(args, build=build)
    finally:
        lease.release()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--root", type=Path, default=Path.cwd(), help="Bento repository root")
    result.add_argument("--workspace", type=Path, help="Repository-local repair workspace")
    result.add_argument("--browser-executable", type=Path)
    result.add_argument("--changed-path", action="append", default=[], help="Implementation path changed during this repair")
    result.add_argument("--run-focused-tests", action="store_true", help="Run only tests selected from the previous failure and changed paths")
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        return run_repair(parser().parse_args(argv))
    except (BentoConverterError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
