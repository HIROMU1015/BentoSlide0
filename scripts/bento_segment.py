"""Insert, append, or explicitly replace converted HTML segments in protected authoring."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from bento_converter.artifact_transaction import ArtifactLeaseConflict, recover_repository_transactions
from bento_converter.browser_check import run_browser_check
from bento_converter.errors import BentoConverterError
from bento_converter.html_document import embed_bento_doc
from bento_converter.html_pipeline import build_from_html
from bento_converter.segment import merge_segment
from bento_converter.work_editor_client import discover_work_editor
from scripts.deck_workflow import WorkflowError, authoring_storage, load_state, repository_root


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--root", type=Path, help="Repository root")
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("import", "append", "insert-before", "insert-after", "replace", "replace-slide", "replace-range", "replace-section"):
        child = commands.add_parser(name)
        child.add_argument("--html", required=True, type=Path)
        child.add_argument("--registry", required=True, type=Path)
        child.add_argument("--browser-executable", type=Path)
        child.add_argument("--skip-browser-check", action="store_true", help="Testing-only: skip browser round-trip evidence")
        if name in {"replace", "replace-slide"}:
            child.add_argument("--slide-id", required=True)
        if name in {"insert-before", "insert-after"}:
            child.add_argument("--anchor-slide-id", required=True)
        if name in {"replace-range", "replace-section"}:
            child.add_argument("--target-slide-id", action="append", required=True, dest="target_slide_ids")
    return result


def _resolve_input(root: Path, value: Path, *, label: str) -> Path:
    path = value.resolve() if value.is_absolute() else (root / value).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise BentoConverterError(f"{label} must remain inside the repository") from exc
    if not path.is_file():
        raise BentoConverterError(f"{label} does not exist: {path}")
    return path


def _protected_artifact_hashes(root: Path, state: dict[str, Any]) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for field in ("generatedHtml", "generatedJson", "generatedRegistry", "finalHtml", "finalJson", "finalRegistry"):
        value = state["outputs"].get(field)
        path = (root / value).resolve() if isinstance(value, str) else None
        result[field] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() if path and path.is_file() else None
    return result


def _save_offline_or_api(
    root: Path, state: dict[str, Any], segment_document: dict[str, Any], segment_registry: dict[str, Any],
    *, operation: str, slide_id: str | None, anchor_slide_id: str | None,
    target_slide_ids: list[str] | None, browser_check: bool, browser_executable: Path | None,
    evidence_root: Path,
) -> dict[str, Any]:
    storage = None
    client = None
    try:
        storage = authoring_storage(root, state)
        storage.acquire_writer_lease()
        current_html, current_document, current_registry = storage.artifact_snapshot()
        status = storage.status()
    except ArtifactLeaseConflict:
        outputs = state["outputs"]
        target = (root / outputs["authoringHtml"]).resolve()
        client = discover_work_editor(root, mode="authoring", target=target)
        snapshot = client.get("/api/document")
        current_html = snapshot.get("serializedHtml")
        current_document = snapshot.get("document")
        current_registry = snapshot.get("registry")
        if not isinstance(current_html, str) or not isinstance(current_document, dict) or not isinstance(current_registry, dict):
            raise BentoConverterError("Authoring Work editor did not provide a complete consistent snapshot")
        status = {
            "documentRevision": snapshot.get("documentRevision"),
            "registryRevision": snapshot.get("registryRevision"),
        }
    try:
        merged_document, merged_registry, report = merge_segment(
            current_document, current_registry, segment_document, segment_registry,
            operation=operation, slide_id=slide_id, anchor_slide_id=anchor_slide_id,
            target_slide_ids=target_slide_ids,
        )
        merged_html = embed_bento_doc(current_html, merged_document)
        if browser_check:
            merged_preview = evidence_root / "merged-preview.bento.html"
            merged_preview.write_bytes(merged_html.encode("utf-8"))
            browser = run_browser_check(
                merged_preview,
                screenshots_dir=evidence_root / "screenshots",
                screenshot_prefix=f"segment-{operation}",
                browser_executable=browser_executable,
            )
            report["browserCheck"] = browser.as_dict()
        else:
            report["browserCheck"] = {"skipped": True}
        report_relative = (evidence_root / "operation-report.json").relative_to(root).as_posix()
        replace_ids = set(target_slide_ids or ())
        if operation in {"replace", "replace-slide"} and slide_id:
            replace_ids.add(slide_id)
        if client is not None:
            saved = client.post("/api/save", {
                "baseDocumentRevision": status["documentRevision"],
                "baseRegistryRevision": status["registryRevision"],
                "serializedHtml": merged_html,
                "registry": merged_registry,
                "replaceSlideIds": sorted(replace_ids),
                "operation": f"segment-{operation}",
                "operationReport": report,
                "reportPath": report_relative,
            })
            writer = "localhost-api"
        else:
            saved = storage.save_serialized(
                merged_html,
                base_document_revision=status["documentRevision"],
                base_registry_revision=status["registryRevision"],
                registry=merged_registry,
                replace_slide_ids=replace_ids,
                operation=f"segment-{operation}", report_details=report,
                report_path=evidence_root / "operation-report.json",
            )
            writer = "offline-transaction"
        return {**saved, "writer": writer, "report": report_relative}
    finally:
        if storage is not None:
            storage.release_writer_lease()


def run(args: argparse.Namespace) -> int:
    root = repository_root(args.root)
    recover_repository_transactions(root)
    state = load_state(root)
    if state.get("schemaVersion") != 2 or state["workflow"]["stage"] != "bento_authoring":
        raise WorkflowError("Segment operations are allowed only in schema v2 bento_authoring stage")
    html_path = _resolve_input(root, args.html, label="Segment HTML")
    registry_path = _resolve_input(root, args.registry, label="Segment registry")
    before = _protected_artifact_hashes(root, state)
    operation_id = uuid.uuid4().hex
    evidence_root = root / "output/segment-reports" / operation_id
    evidence_root.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix=".segment-build-", dir=root / "output") as temporary:
        build_root = Path(temporary)
        converted = build_from_html(
            html_path=html_path, registry_path=registry_path,
            base_path=(root / state["outputs"]["generatedHtml"]).resolve(),
            output_path=build_root / "segment.bento.html",
            browser_executable=args.browser_executable,
            browser_check=not args.skip_browser_check,
        )
        converted_registry = json.loads(
            (build_root / "diagnostics/merged-registry.json").read_text(encoding="utf-8-sig")
        )
        result = _save_offline_or_api(
            root, state, converted.document, converted_registry,
            operation=args.command, slide_id=getattr(args, "slide_id", None),
            anchor_slide_id=getattr(args, "anchor_slide_id", None),
            target_slide_ids=getattr(args, "target_slide_ids", None),
            browser_check=not args.skip_browser_check,
            browser_executable=args.browser_executable, evidence_root=evidence_root,
        )
    after = _protected_artifact_hashes(root, state)
    if after != before:
        raise BentoConverterError("Segment operation changed generated or final artifacts")
    print(json.dumps({"operationId": operation_id, **result}, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run(parser().parse_args(argv))
    except (BentoConverterError, WorkflowError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
