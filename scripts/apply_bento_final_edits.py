"""Apply one validated batch of presentation-only edits to the final Bento deck."""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bento_converter.errors import BentoConverterError
from bento_converter.html_document import embed_bento_doc, extract_bento_doc, load_html
from bento_converter.registry_document import load_registry, registry_revision, validate_registry
from bento_converter.work_editor_storage import (
    PRESENTATION_EDITABLE_ELEMENT_FIELDS,
    WorkEditorStorage,
    document_revision,
    protected_content_fingerprint,
    validate_editor_document,
)
from scripts.deck_workflow import WorkflowError, load_final_baseline, load_state, repository_root


ROOT = Path(__file__).resolve().parents[1]
PATCH_FORMAT = "bento/final-presentation-patch/v1"
RESULT_FORMAT = "bento/final-presentation-edit-result/v1"
SLIDE_EDITABLE_FIELDS = {"background"}
DOCUMENT_EDITABLE_FIELDS = {"theme"}
PATCH_ROOT_FIELDS = {
    "format", "baseRevision", "description", "documentSet",
    "slideEdits", "elementEdits", "zOrders",
}


class FinalEditError(RuntimeError):
    """A requested fast final edit is unsafe or malformed."""


@dataclass(frozen=True)
class FinalEditContext:
    source: Path
    target: Path
    registry: Path | None
    baseline_path: Path | None = None
    baseline_document: dict[str, Any] | None = None
    baseline_fingerprint: str | None = None
    reserved_paths: tuple[Path, ...] = ()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8-sig"),
            parse_constant=lambda token: (_ for _ in ()).throw(ValueError(f"invalid constant {token}")),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise FinalEditError(f"Cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise FinalEditError(f"{label} root must be an object: {path}")
    return value


def _resolve(root: Path, value: str | Path, *, label: str) -> Path:
    candidate = Path(value)
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise FinalEditError(f"{label} must stay inside the repository: {resolved}") from exc
    return resolved


def _sidecar_path(html_path: Path) -> Path:
    name = html_path.name
    if name.endswith(".bento.html"):
        return html_path.with_name(name[: -len(".bento.html")] + ".bento.json")
    return html_path.with_suffix(".bento.json")


def _atomic_write_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(payload.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _validate_report_path(
    report_path: Path, *, root: Path, patch_path: Path, context: FinalEditContext,
) -> None:
    reserved = {
        patch_path.resolve(),
        context.source.resolve(),
        context.target.resolve(),
        _sidecar_path(context.source).resolve(),
        _sidecar_path(context.target).resolve(),
        (root / "deck.yaml").resolve(),
        (context.target.parent / "save-report.json").resolve(),
    }
    if context.registry is not None:
        reserved.add(context.registry.resolve())
    if context.baseline_path is not None:
        reserved.add(context.baseline_path.resolve())
    reserved.update(path.resolve() for path in context.reserved_paths)
    if report_path.resolve() in reserved:
        raise FinalEditError(f"report must not overwrite an input, Bento artifact, registry, or baseline: {report_path}")
    revisions = (context.target.parent / "revisions").resolve()
    try:
        report_path.resolve().relative_to(revisions)
    except ValueError:
        pass
    else:
        raise FinalEditError(f"report must stay outside the immutable revisions directory: {report_path}")
    if report_path.exists():
        existing = _load_json(report_path, label="existing report")
        if existing.get("format") != RESULT_FORMAT:
            raise FinalEditError(f"report refuses to overwrite a non-fast-edit report: {report_path}")


def _index(document: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    slides: dict[str, dict[str, Any]] = {}
    elements: dict[tuple[str, str], dict[str, Any]] = {}
    for slide in document.get("slides", []):
        if not isinstance(slide, dict) or not isinstance(slide.get("id"), str):
            continue
        slides[slide["id"]] = slide
        for element in slide.get("elements", []):
            if isinstance(element, dict) and isinstance(element.get("id"), str):
                elements[(slide["id"], element["id"])] = element
    return slides, elements


def _require_set(value: Any, *, label: str, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise FinalEditError(f"{label}.set must be a non-empty object")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise FinalEditError(f"{label}.set contains protected or unsupported fields: {', '.join(unknown)}")
    for field, item in value.items():
        if isinstance(item, float) and not math.isfinite(item):
            raise FinalEditError(f"{label}.set.{field} must be finite")
    return value


def _require_list(value: Any, *, label: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise FinalEditError(f"{label} must be an array")
    return value


def apply_patch_document(document: dict[str, Any], patch: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if patch.get("format") != PATCH_FORMAT:
        raise FinalEditError(f"patch.format must be {PATCH_FORMAT!r}")
    unknown_root = sorted(set(patch) - PATCH_ROOT_FIELDS)
    if unknown_root:
        raise FinalEditError(f"patch contains unsupported fields: {', '.join(unknown_root)}")
    if "description" in patch and not isinstance(patch["description"], str):
        raise FinalEditError("patch.description must be a string")

    proposed = copy.deepcopy(document)
    slides, elements = _index(proposed)
    changes: list[dict[str, Any]] = []
    seen_slides: set[str] = set()
    seen_elements: set[tuple[str, str]] = set()
    seen_orders: set[str] = set()

    document_set = patch.get("documentSet", {})
    if document_set:
        values = _require_set(document_set, label="documentSet", allowed=DOCUMENT_EDITABLE_FIELDS)
        changed = sorted(field for field, value in values.items() if proposed.get(field) != value)
        for field in changed:
            proposed[field] = copy.deepcopy(values[field])
        if changed:
            changes.append({"scope": "document", "fields": changed})

    for index, edit in enumerate(_require_list(patch.get("slideEdits"), label="slideEdits")):
        label = f"slideEdits[{index}]"
        if not isinstance(edit, dict) or set(edit) != {"slideId", "set"}:
            raise FinalEditError(f"{label} must contain only slideId and set")
        slide_id = edit.get("slideId")
        if not isinstance(slide_id, str) or not slide_id:
            raise FinalEditError(f"{label}.slideId must be a non-empty string")
        if slide_id in seen_slides:
            raise FinalEditError(f"Duplicate slide edit for {slide_id!r}")
        seen_slides.add(slide_id)
        slide = slides.get(slide_id)
        if slide is None:
            raise FinalEditError(f"Unknown slideId {slide_id!r}")
        values = _require_set(edit.get("set"), label=label, allowed=SLIDE_EDITABLE_FIELDS)
        changed = sorted(field for field, value in values.items() if slide.get(field) != value)
        for field in changed:
            slide[field] = copy.deepcopy(values[field])
        if changed:
            changes.append({"scope": "slide", "slideId": slide_id, "fields": changed})

    for index, edit in enumerate(_require_list(patch.get("elementEdits"), label="elementEdits")):
        label = f"elementEdits[{index}]"
        if not isinstance(edit, dict) or set(edit) != {"slideId", "elementId", "set"}:
            raise FinalEditError(f"{label} must contain only slideId, elementId, and set")
        slide_id = edit.get("slideId")
        element_id = edit.get("elementId")
        if not isinstance(slide_id, str) or not slide_id or not isinstance(element_id, str) or not element_id:
            raise FinalEditError(f"{label} requires non-empty slideId and elementId strings")
        key = (slide_id, element_id)
        if key in seen_elements:
            raise FinalEditError(f"Duplicate element edit for {slide_id!r}/{element_id!r}")
        seen_elements.add(key)
        element = elements.get(key)
        if element is None:
            raise FinalEditError(f"Unknown element {slide_id!r}/{element_id!r}")
        values = _require_set(edit.get("set"), label=label, allowed=set(PRESENTATION_EDITABLE_ELEMENT_FIELDS))
        changed = sorted(field for field, value in values.items() if element.get(field) != value)
        for field in changed:
            element[field] = copy.deepcopy(values[field])
        if changed:
            changes.append({"scope": "element", "slideId": slide_id, "elementId": element_id, "fields": changed})

    for index, order in enumerate(_require_list(patch.get("zOrders"), label="zOrders")):
        label = f"zOrders[{index}]"
        if not isinstance(order, dict) or set(order) != {"slideId", "elementIds"}:
            raise FinalEditError(f"{label} must contain only slideId and elementIds")
        slide_id = order.get("slideId")
        element_ids = order.get("elementIds")
        if not isinstance(slide_id, str) or not slide_id or not isinstance(element_ids, list):
            raise FinalEditError(f"{label} requires slideId and an elementIds array")
        if slide_id in seen_orders:
            raise FinalEditError(f"Duplicate z-order edit for {slide_id!r}")
        seen_orders.add(slide_id)
        slide = slides.get(slide_id)
        if slide is None:
            raise FinalEditError(f"Unknown slideId {slide_id!r}")
        current_elements = slide.get("elements", [])
        current_ids = [item.get("id") for item in current_elements if isinstance(item, dict)]
        if len(element_ids) != len(set(element_ids)):
            raise FinalEditError(f"{label}.elementIds must not contain duplicates")
        if set(element_ids) != set(current_ids) or len(element_ids) != len(current_ids):
            raise FinalEditError(f"{label}.elementIds must list every existing element exactly once")
        if element_ids != current_ids:
            by_id = {item["id"]: item for item in current_elements}
            slide["elements"] = [by_id[element_id] for element_id in element_ids]
            changes.append({"scope": "zOrder", "slideId": slide_id, "fields": ["elements"]})

    if not changes:
        return proposed, []
    proposed["modified"] = _utc_now()
    return proposed, changes


def apply_final_edits(
    *, source: Path, target: Path, registry: Path | None, patch: dict[str, Any], dry_run: bool = False,
    baseline_document: dict[str, Any] | None = None, baseline_fingerprint: str | None = None,
) -> dict[str, Any]:
    source = source.resolve()
    target = target.resolve()
    if source == target:
        raise FinalEditError("Generated source and final target must be different files")
    if not source.is_file():
        raise FinalEditError(f"Generated source does not exist: {source}")
    if not target.is_file():
        raise FinalEditError(f"Final target does not exist: {target}. Initialize finalization first")
    if registry is not None and not registry.resolve().is_file():
        raise FinalEditError(f"Registry does not exist: {registry.resolve()}")
    if (baseline_document is None) != (baseline_fingerprint is None):
        raise FinalEditError("Immutable baseline document and fingerprint must be supplied together")

    registry_document = _load_json(registry.resolve(), label="registry") if registry is not None else {
        "protected": {}, "equations": {}, "figures": {}, "charts": {}, "tables": {},
    }
    before_html = load_html(target)
    before_document = extract_bento_doc(before_html)
    comparison_document = baseline_document if baseline_document is not None else before_document
    comparison_fingerprint = (
        baseline_fingerprint if baseline_fingerprint is not None
        else protected_content_fingerprint(before_document)
    )
    validate_editor_document(
        before_document, current=comparison_document, registry=registry_document, allow_content_edit=False,
    )
    if protected_content_fingerprint(before_document) != comparison_fingerprint:
        raise FinalEditError("Current final content/structure differs from its immutable finalization baseline")

    storage = WorkEditorStorage(source=source, target=target, registry=registry)
    before = storage.document_response()
    expected_revision = patch.get("baseRevision")
    if expected_revision is not None:
        if not isinstance(expected_revision, str):
            raise FinalEditError("patch.baseRevision must be a string")
        if expected_revision != before["revision"]:
            raise FinalEditError(
                f"patch.baseRevision is stale: expected {before['revision']}, received {expected_revision}"
            )

    proposed, changes = apply_patch_document(before["document"], patch)
    validate_editor_document(
        proposed, current=comparison_document, registry=registry_document, allow_content_edit=False,
    )
    if protected_content_fingerprint(proposed) != comparison_fingerprint:
        raise FinalEditError("Proposed final content/structure differs from its immutable finalization baseline")
    if not changes:
        status = storage.status()
        return {
            "format": RESULT_FORMAT,
            "saved": False,
            "dryRun": dry_run,
            "noOp": True,
            "target": str(target),
            "baseRevision": before["revision"],
            "revision": before["revision"],
            "validation": status["validation"],
            "backupCount": status["backupCount"],
            "runtimeFingerprint": status["runtimeFingerprint"],
            "protectedContentFingerprintUnchanged": True,
            "htmlJsonEqual": extract_bento_doc(before_html) == json.loads(_sidecar_path(target).read_text(encoding="utf-8")),
            "changes": [],
        }

    serialized = embed_bento_doc(before_html, proposed)
    proposed_revision = document_revision(proposed)
    if dry_run:
        validation = storage.validate_serialized(serialized)
        status = storage.status()
        result = {
            "validation": validation["validation"],
            "backupCount": status["backupCount"],
            "runtimeFingerprint": status["runtimeFingerprint"],
        }
    else:
        result = storage.save_serialized(serialized, base_revision=before["revision"])

    final_document = proposed if dry_run else storage.document_response()["document"]
    validate_editor_document(
        final_document, current=comparison_document, registry=registry_document, allow_content_edit=False,
    )
    final_html = before_html if dry_run else load_html(target)
    sidecar_document = json.loads(_sidecar_path(target).read_text(encoding="utf-8"))
    return {
        "format": RESULT_FORMAT,
        "saved": not dry_run,
        "dryRun": dry_run,
        "noOp": False,
        "target": str(target),
        "baseRevision": before["revision"],
        "revision": proposed_revision,
        "validation": result["validation"],
        "backupCount": result["backupCount"],
        "runtimeFingerprint": result["runtimeFingerprint"],
        "protectedContentFingerprintUnchanged": protected_content_fingerprint(final_document) == comparison_fingerprint,
        "htmlJsonEqual": extract_bento_doc(final_html) == sidecar_document,
        "changes": changes,
    }


def _paths(args: argparse.Namespace) -> FinalEditContext:
    root = repository_root(args.root)
    if bool(args.source) != bool(args.target):
        raise FinalEditError("Use --source and --target together")
    if args.source:
        source = _resolve(root, args.source, label="source")
        target = _resolve(root, args.target, label="target")
        state = None
    else:
        state = load_state(root)
        stage = state["workflow"]["stage"]
        if stage not in {"bento_finalization", "complete"}:
            raise FinalEditError(
                f"deck.yaml stage is {stage!r}; fast final editing requires 'bento_finalization' or 'complete'"
            )
        source_field = (
            "authoringHtml"
            if state.get("schemaVersion") == 2 and state["outputs"].get("authoringHtml")
            else "generatedHtml"
        )
        source = _resolve(root, state["outputs"][source_field], label=f"outputs.{source_field}")
        target = _resolve(root, state["outputs"]["finalHtml"], label="outputs.finalHtml")
    if args.registry:
        registry: Path | None = _resolve(root, args.registry, label="registry")
    else:
        candidate = (
            _resolve(root, state["outputs"]["finalRegistry"], label="outputs.finalRegistry")
            if state is not None and state.get("schemaVersion") == 2
            else source.parent / "diagnostics" / "merged-registry.json"
        )
        if state is not None and not candidate.is_file():
            label = "final registry" if state.get("schemaVersion") == 2 else "merged registry"
            raise FinalEditError(f"Required {label} does not exist: {candidate}")
        registry = candidate if candidate.is_file() else None
    if state is None:
        return FinalEditContext(source=source, target=target, registry=registry)

    source_document = extract_bento_doc(load_html(source))
    baseline_document, baseline_fingerprint = load_final_baseline(root, state, source_document)
    metadata = state["validation"]["finalBaseline"]
    baseline_field = "documentPath" if state.get("schemaVersion") == 2 else "path"
    baseline_path = _resolve(
        root, metadata[baseline_field], label=f"validation.finalBaseline.{baseline_field}",
    )
    if state.get("schemaVersion") == 2:
        if registry is None:
            raise FinalEditError("Schema v2 fast final editing requires the frozen final registry")
        registry_document = load_registry(registry)
        validate_registry(registry_document, allow_v1=False)
        if registry_revision(registry_document) != metadata["registryRevision"]:
            raise FinalEditError("Final registry revision does not match the immutable baseline metadata")
        registry_baseline = _resolve(
            root, metadata["registryPath"], label="validation.finalBaseline.registryPath",
        )
        if not registry_baseline.is_file():
            raise FinalEditError(f"Final registry baseline does not exist: {registry_baseline}")
        baseline_registry_document = load_registry(registry_baseline)
        validate_registry(baseline_registry_document, allow_v1=False)
        if (
            registry_revision(baseline_registry_document) != metadata["registryRevision"]
            or registry_revision(baseline_registry_document) != registry_revision(registry_document)
        ):
            raise FinalEditError("Final registry differs from its immutable registry baseline")
    reserved_paths = tuple(
        _resolve(root, value, label=f"outputs.{field}")
        for field, value in state["outputs"].items()
        if isinstance(value, str)
    )
    if state.get("schemaVersion") == 2:
        reserved_paths += (_resolve(
            root, metadata["registryPath"], label="validation.finalBaseline.registryPath",
        ),)
    return FinalEditContext(
        source=source,
        target=target,
        registry=registry,
        baseline_path=baseline_path,
        baseline_document=baseline_document,
        baseline_fingerprint=baseline_fingerprint,
        reserved_paths=reserved_paths,
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--patch", required=True, type=Path, help="JSON presentation-edit patch")
    result.add_argument("--root", type=Path, default=ROOT, help="Repository root")
    result.add_argument("--source", type=Path, help="Explicit immutable-runtime Bento HTML; use with --target")
    result.add_argument("--target", type=Path, help="Explicit final Bento HTML; use with --source")
    result.add_argument("--registry", type=Path, help="Explicit registry JSON for final validation")
    result.add_argument("--dry-run", action="store_true", help="Validate the batch without saving")
    result.add_argument("--report", type=Path, help="Optional JSON result path")
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        root = repository_root(args.root)
        patch_path = _resolve(root, args.patch, label="patch")
        patch = _load_json(patch_path, label="patch")
        context = _paths(args)
        report_path = _resolve(root, args.report, label="report") if args.report else None
        if report_path is not None:
            _validate_report_path(report_path, root=root, patch_path=patch_path, context=context)
        result = apply_final_edits(
            source=context.source,
            target=context.target,
            registry=context.registry,
            patch=patch,
            dry_run=args.dry_run,
            baseline_document=context.baseline_document,
            baseline_fingerprint=context.baseline_fingerprint,
        )
        payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if report_path is not None:
            _atomic_write_text(report_path, payload)
        print(payload, end="")
        return 0
    except (FinalEditError, WorkflowError, BentoConverterError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
