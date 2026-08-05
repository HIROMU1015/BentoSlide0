"""Validate and advance the repository-centered BentoSlide workflow state."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from bento_converter.artifact_transaction import ArtifactTransactionStore, recover_repository_transactions
from bento_converter.authoring_storage import AuthoringArtifactStorage
from bento_converter.bento_validator import validate_bento_doc
from bento_converter.errors import BentoConverterError
from bento_converter.html_document import extract_bento_doc, load_html, runtime_fingerprint, serialize_bento_doc
from bento_converter.html_source import REGISTRY_FORMAT
from bento_converter.registry_document import (
    canonical_registry_json,
    load_registry,
    normalize_registry,
    registry_revision,
    validate_registry,
)
from bento_converter.section_approval import SectionApprovalEvidence, compute_section_approval_evidence
from bento_converter.work_editor_storage import (
    WorkEditorStorage,
    document_revision,
    protected_content_fingerprint,
    validate_editor_document,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_RELATIVE = Path("workflow/deck.schema.json")
LEGACY_SCHEMA_RELATIVE = Path("workflow/deck.v1.schema.json")
STATE_RELATIVE = Path("deck.yaml")
SOURCE_MANIFEST_FORMAT = 1
CHAPTER_PATTERN = re.compile(r"^chapter-[0-9]{2,}$")
PLAN_FILES = {
    "explanationPolicy": Path("planning/explanation-policy.md"),
    "storyOutline": Path("planning/story-outline.md"),
    "slidePlan": Path("planning/slide-plan.md"),
}
STAGE_OWNER = {
    "initialized": "work",
    "planning": "work",
    "awaiting_plan_approval": "work",
    "html_authoring": "work",
    "html_review": "work",
    "ready_for_conversion": "codex",
    "converting": "codex",
    "bento_validation": "codex",
    "bento_authoring": "work",
    "content_review": "work",
    "bento_finalization": "work",
    "complete": "codex",
}
STAGE_SOURCE = {
    "initialized": "sources",
    "planning": "planning",
    "awaiting_plan_approval": "planning",
    "html_authoring": "html",
    "html_review": "html",
    "ready_for_conversion": "html",
    "converting": "html",
    "bento_validation": "generated",
    "bento_authoring": "authoring",
    "content_review": "authoring",
    "bento_finalization": "final",
    "complete": "final",
}

LEGACY_STAGE_SOURCE = {
    **STAGE_SOURCE,
    "html_authoring": "chapters",
    "html_review": "chapters",
    "ready_for_conversion": "chapters",
    "converting": "chapters",
}


class WorkflowError(RuntimeError):
    """A requested workflow operation is unsafe or invalid."""


class ChapterHtmlParser(HTMLParser):
    """Collect stable IDs and registry references without rendering the chapter."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.current_slide: str | None = None
        self.slide_ids: list[str] = []
        self.elements: dict[str, list[str]] = {}
        self.references: list[tuple[str, str, str | None, str | None]] = []
        self.text_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value for key, value in attrs}
        slide_id = values.get("data-slide-id")
        if slide_id:
            self.current_slide = slide_id
            self.slide_ids.append(slide_id)
            self.elements.setdefault(slide_id, [])
        element_id = values.get("data-bento-id")
        if element_id:
            if not self.current_slide:
                raise WorkflowError(f"data-bento-id={element_id!r} is outside a data-slide-id section")
            self.elements.setdefault(self.current_slide, []).append(element_id)
        for attribute, collection in (
            ("data-equation-id", "equations"),
            ("data-figure-id", "figures"),
            ("data-chart-id", "charts"),
            ("data-table-id", "tables"),
            ("data-asset-id", "assets"),
        ):
            reference = values.get(attribute)
            if reference:
                self.references.append((collection, reference, self.current_slide, values.get("data-latex")))

    handle_startendtag = handle_starttag

    def handle_data(self, data: str) -> None:
        self.text_chunks.append(data)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def repository_root(value: str | Path | None = None) -> Path:
    root = Path(value).resolve() if value else ROOT.resolve()
    if not root.is_dir():
        raise WorkflowError(f"Repository does not exist: {root}")
    return root


def _repo_path(root: Path, value: str, *, field: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        raise WorkflowError(f"{field} must be repository-relative: {value}")
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise WorkflowError(f"{field} escapes the repository: {value}") from exc
    return resolved


def _sidecar_path(html_path: Path) -> Path:
    name = html_path.name
    if name.endswith(".bento.html"):
        return html_path.with_name(name[: -len(".bento.html")] + ".bento.json")
    return html_path.with_suffix(".bento.json")


def _final_baseline_path(root: Path, state: dict[str, Any]) -> Path:
    final_html = _repo_path(root, state["outputs"]["finalHtml"], field="outputs.finalHtml")
    name = final_html.name
    stem = name[: -len(".bento.html")] if name.endswith(".bento.html") else final_html.stem
    return final_html.parent / "revisions" / f"{stem}.baseline.bento.json"


def _final_registry_baseline_path(root: Path, state: dict[str, Any]) -> Path:
    final_html = _repo_path(root, state["outputs"]["finalHtml"], field="outputs.finalHtml")
    name = final_html.name
    stem = name[: -len(".bento.html")] if name.endswith(".bento.html") else final_html.stem
    return final_html.parent / "revisions" / f"{stem}.baseline.registry.json"


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"Cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise WorkflowError(f"{label} root must be an object: {path}")
    return value


def _atomic_write_bytes(destination: Path, payload: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent, delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _atomic_write_json(destination: Path, value: dict[str, Any]) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    _atomic_write_bytes(destination, payload)


def load_source_manifest(root: Path, state: dict[str, Any], *, require_exists: bool = True) -> dict[str, Any]:
    if state.get("schemaVersion") != 2:
        raise WorkflowError("Source manifests require deck schema v2")
    path = _repo_path(root, state["sources"]["manifest"], field="sources.manifest")
    if not path.is_file():
        if require_exists:
            raise WorkflowError(f"Source manifest does not exist: {state['sources']['manifest']}")
        return {"schemaVersion": SOURCE_MANIFEST_FORMAT, "authorityMode": state["sources"]["authorityMode"], "items": []}
    try:
        manifest = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise WorkflowError(f"Cannot read source manifest {path}: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != SOURCE_MANIFEST_FORMAT:
        raise WorkflowError("Source manifest schemaVersion must be 1")
    if manifest.get("authorityMode") not in {"single", "multiple", "imported"}:
        raise WorkflowError("Source manifest authorityMode is invalid")
    if manifest["authorityMode"] != state["sources"]["authorityMode"]:
        raise WorkflowError("Source manifest authorityMode differs from deck.yaml")
    items = manifest.get("items")
    if not isinstance(items, list):
        raise WorkflowError("Source manifest items must be an array")
    seen: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise WorkflowError(f"Source manifest item {index} must be an object")
        source_id = item.get("id")
        if not isinstance(source_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", source_id):
            raise WorkflowError(f"Source manifest item {index} has an invalid id")
        if source_id in seen:
            raise WorkflowError(f"Duplicate source manifest id: {source_id}")
        seen.add(source_id)
        source_path = item.get("path")
        if not isinstance(source_path, str) or not source_path:
            raise WorkflowError(f"Source manifest item {source_id!r} requires a path")
        resolved_source = _repo_path(root, source_path, field=f"sources.items[{index}].path")
        if require_exists and not resolved_source.exists():
            raise WorkflowError(f"Source manifest item does not exist: {source_path}")
        if not isinstance(item.get("type"), str) or not item["type"]:
            raise WorkflowError(f"Source manifest item {source_id!r} requires a type")
        if item.get("role") not in {"primary", "evidence", "reference", "supplementary", "imported"}:
            raise WorkflowError(f"Source manifest item {source_id!r} has an invalid role")
    if manifest["authorityMode"] == "single":
        primaries = [item for item in items if item.get("role") == "primary"]
        if len(primaries) > 1:
            raise WorkflowError("Single-authority source manifest has multiple primary items")
    return manifest


def content_approval_digest(document_revision_value: str, registry_revision_value: str) -> str:
    for label, value in (("document", document_revision_value), ("registry", registry_revision_value)):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
            raise WorkflowError(f"Invalid {label} revision for content approval")
    payload = (
        "bento/content-approval/v1\0" + document_revision_value + "\0" + registry_revision_value
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def load_state(root: Path) -> dict[str, Any]:
    path = root / STATE_RELATIVE
    try:
        state = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise WorkflowError(f"Cannot read deck.yaml: {exc}") from exc
    if not isinstance(state, dict):
        raise WorkflowError("deck.yaml root must be a mapping")
    validate_state(root, state)
    return state


def validate_state(root: Path, state: dict[str, Any]) -> None:
    version = state.get("schemaVersion")
    if version not in {1, 2}:
        raise WorkflowError(f"Unsupported deck.yaml schemaVersion: {version!r}")
    schema_path = root / (LEGACY_SCHEMA_RELATIVE if version == 1 else SCHEMA_RELATIVE)
    schema = _read_json(schema_path, label="deck schema")
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(state),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        formatted = []
        for error in errors:
            location = ".".join(str(part) for part in error.absolute_path) or "deck"
            formatted.append(f"{location}: {error.message}")
        raise WorkflowError("deck.yaml schema validation failed:\n- " + "\n- ".join(formatted))

    workflow = state["workflow"]
    stage = workflow["stage"]
    stage_sources = LEGACY_STAGE_SOURCE if version == 1 else STAGE_SOURCE
    if stage != "blocked":
        if workflow["owner"] != STAGE_OWNER[stage]:
            raise WorkflowError(f"workflow.owner must be {STAGE_OWNER[stage]!r} for stage {stage!r}")
        if workflow["sourceOfTruth"] != stage_sources[stage]:
            raise WorkflowError(f"workflow.sourceOfTruth must be {stage_sources[stage]!r} for stage {stage!r}")
    current = workflow["currentChapter"]
    if current is not None and current not in state["chapters"]:
        raise WorkflowError(f"workflow.currentChapter is not registered: {current}")
    if version == 2:
        current_section = workflow["currentSection"]
        if current_section is not None and current_section not in state["sections"]:
            raise WorkflowError(f"workflow.currentSection is not registered: {current_section}")
        if state["authoring"]["currentSection"] != current_section:
            raise WorkflowError("authoring.currentSection must match workflow.currentSection")
    blocked_from = workflow.get("blockedFrom")
    if stage == "blocked":
        if not workflow["blockingReason"]:
            raise WorkflowError("workflow.blockingReason is required while blocked")
        if not isinstance(blocked_from, dict):
            raise WorkflowError("workflow.blockedFrom is required while blocked")
        previous_stage = blocked_from["stage"]
        if previous_stage in {"blocked", "complete"}:
            raise WorkflowError(f"workflow.blockedFrom.stage cannot be {previous_stage!r}")
        if blocked_from["owner"] != STAGE_OWNER[previous_stage]:
            raise WorkflowError("workflow.blockedFrom.owner does not match its stage")
        if blocked_from["sourceOfTruth"] != stage_sources[previous_stage]:
            raise WorkflowError("workflow.blockedFrom.sourceOfTruth does not match its stage")
        previous_current = blocked_from["currentChapter"]
        if previous_current is not None and previous_current not in state["chapters"]:
            raise WorkflowError(f"workflow.blockedFrom.currentChapter is not registered: {previous_current}")
        if version == 2:
            previous_section = blocked_from["currentSection"]
            if previous_section is not None and previous_section not in state["sections"]:
                raise WorkflowError(f"workflow.blockedFrom.currentSection is not registered: {previous_section}")
    elif workflow["blockingReason"] is not None or blocked_from is not None:
        raise WorkflowError("workflow.blockingReason and blockedFrom must be null outside the blocked stage")
    for field in ("request",):
        _repo_path(root, state["project"][field], field=f"project.{field}")
    if state["project"]["primarySource"]:
        _repo_path(root, state["project"]["primarySource"], field="project.primarySource")
    for index, value in enumerate(state["project"]["supplementarySources"]):
        _repo_path(root, value, field=f"project.supplementarySources[{index}]")
    for chapter_id, chapter in state["chapters"].items():
        if not CHAPTER_PATTERN.fullmatch(chapter_id):
            raise WorkflowError(f"Invalid chapter id: {chapter_id}")
        _repo_path(root, chapter["html"], field=f"chapters.{chapter_id}.html")
        _repo_path(root, chapter["registry"], field=f"chapters.{chapter_id}.registry")
    if version == 2:
        manifest_path = _repo_path(root, state["sources"]["manifest"], field="sources.manifest")
        if manifest_path.is_file():
            load_source_manifest(root, state)
        mode = state["authoring"]["mode"]
        if mode == "modular":
            if state["authoring"]["entryHtml"] is not None or state["authoring"]["registry"] is not None:
                raise WorkflowError("Modular authoring must use chapters rather than authoring.entryHtml/registry")
        else:
            if state["authoring"]["entryHtml"] is None or state["authoring"]["registry"] is None:
                raise WorkflowError(f"{mode} authoring requires entryHtml and registry")
            _repo_path(root, state["authoring"]["entryHtml"], field="authoring.entryHtml")
            _repo_path(root, state["authoring"]["registry"], field="authoring.registry")
            if state["chapters"]:
                raise WorkflowError(f"{mode} authoring must use sections rather than chapters")
            if workflow["currentChapter"] is not None:
                raise WorkflowError(f"{mode} authoring cannot have a current chapter")
            registered_slides: set[str] = set()
            for section_id, section in state["sections"].items():
                duplicates = registered_slides.intersection(section["slideIds"])
                if duplicates:
                    raise WorkflowError(f"Slide IDs are registered in multiple sections: {sorted(duplicates)}")
                registered_slides.update(section["slideIds"])
                if section["status"] == "approved" and section["approvalDigest"] is None:
                    raise WorkflowError(f"Approved section has no approval digest: {section_id}")
                if section["status"] != "approved" and section["approvalDigest"] is not None:
                    raise WorkflowError(f"Unapproved section retains an approval digest: {section_id}")
    resolved_outputs = {
        field: _repo_path(root, value, field=f"outputs.{field}")
        for field, value in state["outputs"].items() if value is not None
    }
    if len(set(resolved_outputs.values())) != len(resolved_outputs):
        raise WorkflowError("Generated/authoring/final Bento and registry output paths must be distinct")
    if resolved_outputs["generatedJson"] != _sidecar_path(resolved_outputs["generatedHtml"]):
        raise WorkflowError("outputs.generatedJson must be the sidecar path derived from outputs.generatedHtml")
    if resolved_outputs["finalJson"] != _sidecar_path(resolved_outputs["finalHtml"]):
        raise WorkflowError("outputs.finalJson must be the sidecar path derived from outputs.finalHtml")
    if version == 2:
        authoring_values = [state["outputs"][field] for field in ("authoringHtml", "authoringJson", "authoringRegistry")]
        if any(value is None for value in authoring_values) and not all(value is None for value in authoring_values):
            raise WorkflowError("Authoring HTML, JSON, and registry paths must all be set or all be null")
        late_compatibility = state["migration"]["lateStageCompatibility"]
        if all(value is None for value in authoring_values):
            late_stage = stage in {"bento_finalization", "complete"} or (
                stage == "blocked" and blocked_from["stage"] == "bento_finalization"
            )
            if not late_compatibility or not late_stage:
                raise WorkflowError("Authoring outputs may be null only for migrated late-stage decks")
        else:
            if resolved_outputs["authoringJson"] != _sidecar_path(resolved_outputs["authoringHtml"]):
                raise WorkflowError("outputs.authoringJson must be the sidecar path derived from outputs.authoringHtml")
        if state["outputs"]["finalRegistry"] is None:
            raise WorkflowError("outputs.finalRegistry is required in schema v2")
        approval = state["approvals"]["bentoContent"]
        approval_values = (
            approval["documentRevision"], approval["registryRevision"],
            approval["approvalDigest"], approval["approvedAt"],
        )
        if approval["status"] == "pending" and any(value is not None for value in approval_values):
            raise WorkflowError("Pending Bento content approval must not retain revision metadata")
        if approval["status"] == "approved":
            if any(value is None for value in approval_values):
                raise WorkflowError("Approved Bento content requires document/registry revisions, digest, and timestamp")
            expected_digest = content_approval_digest(approval["documentRevision"], approval["registryRevision"])
            if approval["approvalDigest"] != expected_digest:
                raise WorkflowError("Bento content approval digest does not match its revisions")
    baseline = state["validation"].get("finalBaseline")
    if baseline is not None:
        document_field = "path" if version == 1 else "documentPath"
        baseline_path = _repo_path(root, baseline[document_field], field=f"validation.finalBaseline.{document_field}")
        if baseline_path != _final_baseline_path(root, state):
            raise WorkflowError(f"validation.finalBaseline.{document_field} does not match outputs.finalHtml")
        if version == 2:
            registry_path = _repo_path(root, baseline["registryPath"], field="validation.finalBaseline.registryPath")
            if registry_path != _final_registry_baseline_path(root, state):
                raise WorkflowError("validation.finalBaseline.registryPath does not match outputs.finalHtml")
    current_url = state["preview"]["currentUrl"]
    if current_url:
        port = int(current_url.rsplit(":", 1)[1].rstrip("/"))
        if port < 1 or port > 65535:
            raise WorkflowError("preview.currentUrl contains an invalid port")


def atomic_write_state(root: Path, state: dict[str, Any]) -> None:
    validate_state(root, state)
    destination = root / STATE_RELATIVE
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = yaml.safe_dump(state, allow_unicode=True, sort_keys=False).encode("utf-8")
    handle = tempfile.NamedTemporaryFile(prefix=".deck.", suffix=".yaml.tmp", dir=destination.parent, delete=False)
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        if os.name != "nt":
            descriptor = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def append_work_log(root: Path, message: str) -> None:
    path = root / "planning/work-log.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("# Work log\n\n", encoding="utf-8")
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(f"- {utc_now()} — {message}\n")


def _transition(state: dict[str, Any], stage: str, status: str, *, current: str | None = None) -> None:
    workflow = state["workflow"]
    workflow["stage"] = stage
    workflow["status"] = status
    workflow["owner"] = STAGE_OWNER[stage]
    workflow["sourceOfTruth"] = (
        LEGACY_STAGE_SOURCE[stage] if state.get("schemaVersion") == 1 else STAGE_SOURCE[stage]
    )
    if state.get("schemaVersion") == 2:
        if state["authoring"]["mode"] == "modular":
            workflow["currentChapter"] = current
            workflow["currentSection"] = None
            state["authoring"]["currentSection"] = None
        else:
            workflow["currentChapter"] = None
            workflow["currentSection"] = current
            state["authoring"]["currentSection"] = current
    else:
        workflow["currentChapter"] = current
    workflow["blockingReason"] = None
    workflow["blockedFrom"] = None


def _require_stage(state: dict[str, Any], *allowed: str) -> None:
    actual = state["workflow"]["stage"]
    if actual not in allowed:
        raise WorkflowError(f"Stage {actual!r} does not allow this operation; expected one of {allowed}")


def discover_source_candidates(root: Path, state: dict[str, Any]) -> tuple[Path | None, list[Path]]:
    if state.get("schemaVersion") == 2:
        manifest = load_source_manifest(root, state)
        candidates = [
            _repo_path(root, item["path"], field=f"sources.items.{item['id']}.path")
            for item in manifest["items"]
        ]
        primaries = [
            _repo_path(root, item["path"], field=f"sources.items.{item['id']}.path")
            for item in manifest["items"] if item.get("role") == "primary"
        ]
        if manifest["authorityMode"] == "single" and len(primaries) != 1:
            raise WorkflowError("Single-authority source manifest requires exactly one primary item")
        return (primaries[0] if len(primaries) == 1 else None), candidates
    sources = (root / "sources").resolve()
    if not sources.is_dir():
        raise WorkflowError("sources/ does not exist")
    candidates: list[Path] = []
    for path in sources.rglob("*.pdf"):
        if not path.is_file():
            continue
        resolved = path.resolve()
        try:
            resolved.relative_to(sources)
        except ValueError:
            continue
        candidates.append(resolved)
    candidates.sort(key=lambda path: path.relative_to(root).as_posix().casefold())

    explicit = state["project"].get("primarySource")
    if explicit:
        selected = _repo_path(root, explicit, field="project.primarySource")
        if selected.suffix.casefold() != ".pdf" or not selected.is_file():
            raise WorkflowError(f"Configured primarySource is not an existing PDF: {explicit}")
        if selected not in candidates:
            raise WorkflowError(f"Configured primarySource must remain under sources/: {explicit}")
        return selected, candidates
    if len(candidates) == 1:
        return candidates[0], candidates
    if not candidates:
        raise WorkflowError("No primary PDF was found under sources/. Put it in sources/private/.")
    display = ", ".join(path.relative_to(root).as_posix() for path in candidates)
    raise WorkflowError(f"Multiple PDF sources found; set project.primarySource in deck.yaml: {display}")


def _meaningful_markdown(path: Path) -> bool:
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8-sig")
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"^\s*#+\s+.*$", "", text, flags=re.MULTILINE)
    return bool(text.strip())


def validate_planning(root: Path) -> None:
    missing = [str(path) for path in PLAN_FILES.values() if not _meaningful_markdown(root / path)]
    if missing:
        raise WorkflowError("Planning artifacts are missing substantive content: " + ", ".join(missing))


def _load_chapter(root: Path, chapter_id: str, entry: dict[str, Any]) -> tuple[ChapterHtmlParser, dict[str, Any]]:
    html_path = _repo_path(root, entry["html"], field=f"chapters.{chapter_id}.html")
    registry_path = _repo_path(root, entry["registry"], field=f"chapters.{chapter_id}.registry")
    if not html_path.is_file():
        raise WorkflowError(f"Chapter HTML does not exist: {entry['html']}")
    if not registry_path.is_file():
        raise WorkflowError(f"Chapter registry does not exist: {entry['registry']}")
    registry = _read_json(registry_path, label="chapter registry")
    if registry.get("format") != REGISTRY_FORMAT:
        raise WorkflowError(f"{entry['registry']}: format must be {REGISTRY_FORMAT!r}")
    if registry.get("chapterId") != chapter_id:
        raise WorkflowError(f"{entry['registry']}: chapterId must be {chapter_id!r}")
    parser = ChapterHtmlParser()
    try:
        html_source = html_path.read_text(encoding="utf-8-sig")
        parser.feed(html_source)
        parser.close()
    except (OSError, UnicodeDecodeError) as exc:
        raise WorkflowError(f"Cannot read chapter HTML {entry['html']}: {exc}") from exc
    if not parser.slide_ids:
        raise WorkflowError(f"Chapter contains no data-slide-id sections: {entry['html']}")
    duplicate_slides = sorted({value for value in parser.slide_ids if parser.slide_ids.count(value) > 1})
    if duplicate_slides:
        raise WorkflowError(f"Duplicate slide IDs in {entry['html']}: {duplicate_slides}")
    for slide_id, values in parser.elements.items():
        duplicate_elements = sorted({value for value in values if values.count(value) > 1})
        if duplicate_elements:
            raise WorkflowError(f"Duplicate element IDs in slide {slide_id}: {duplicate_elements}")
    definitions = {name: registry.get(name, {}) for name in ("assets", "equations", "figures", "charts", "tables")}
    for collection, reference, slide_id, latex in parser.references:
        if not isinstance(definitions[collection], dict) or reference not in definitions[collection]:
            raise WorkflowError(f"{entry['html']}: {collection}.{reference} is not defined in the paired registry")
        if collection == "equations" and latex is not None:
            expected = definitions[collection][reference].get("latex") if isinstance(definitions[collection][reference], dict) else None
            if latex.strip() != str(expected).strip():
                raise WorkflowError(f"Equation {reference} data-latex does not match registry latex")
        definition = definitions[collection].get(reference)
        if collection == "equations" and isinstance(definition, dict) and "usedOnSlides" in definition:
            used = definition["usedOnSlides"]
            if not isinstance(used, list) or slide_id not in used:
                raise WorkflowError(f"Equation {reference} does not list slide {slide_id} in usedOnSlides")
    protected = registry.get("protected", {})
    if protected and not isinstance(protected, dict):
        raise WorkflowError(f"{entry['registry']}: protected must be an object")
    all_elements = {value for values in parser.elements.values() for value in values}
    for slide_id in protected.get("slideIds", []):
        if slide_id not in parser.slide_ids:
            raise WorkflowError(f"Registry-protected slide is absent from HTML: {slide_id}")
    for element_id in protected.get("elementIds", []):
        if element_id not in all_elements:
            raise WorkflowError(f"Registry-protected element is absent from HTML: {element_id}")
    for required in protected.get("requiredText", []):
        if required not in "".join(parser.text_chunks):
            raise WorkflowError(f"Registry-protected required text is absent from HTML: {required}")
    return parser, registry


def validate_chapters(root: Path, state: dict[str, Any], *, require_complete: bool = False) -> None:
    if not state["chapters"]:
        raise WorkflowError("No chapters are registered in deck.yaml")
    all_slides: set[str] = set()
    for chapter_id in sorted(state["chapters"]):
        entry = state["chapters"][chapter_id]
        if require_complete and (entry["status"] != "complete" or entry["visualApproval"] != "approved"):
            raise WorkflowError(f"Chapter is not complete and visually approved: {chapter_id}")
        parser, _ = _load_chapter(root, chapter_id, entry)
        duplicates = all_slides.intersection(parser.slide_ids)
        if duplicates:
            raise WorkflowError(f"Slide IDs are duplicated across chapters: {sorted(duplicates)}")
        all_slides.update(parser.slide_ids)


def load_single_section_evidence(
    root: Path, state: dict[str, Any],
) -> dict[str, SectionApprovalEvidence]:
    if state.get("schemaVersion") != 2 or state["authoring"]["mode"] not in {"single", "imported"}:
        raise WorkflowError("Single section validation requires schema v2 single/imported authoring")
    html_path = _repo_path(root, state["authoring"]["entryHtml"], field="authoring.entryHtml")
    registry_path = _repo_path(root, state["authoring"]["registry"], field="authoring.registry")
    if not registry_path.is_file():
        raise WorkflowError(f"Authoring registry does not exist: {state['authoring']['registry']}")
    registry = _read_json(registry_path, label="single HTML registry")
    try:
        validate_registry(registry, allow_v1=True)
        return compute_section_approval_evidence(html_path, registry, repository=root)
    except BentoConverterError as exc:
        raise WorkflowError(str(exc)) from exc


def validate_sections(root: Path, state: dict[str, Any], *, require_approved: bool = False) -> dict[str, SectionApprovalEvidence]:
    if not state["sections"]:
        raise WorkflowError("No sections are registered in deck.yaml")
    evidence = load_single_section_evidence(root, state)
    registered = set(state["sections"])
    discovered = set(evidence)
    if registered != discovered:
        raise WorkflowError(
            f"HTML/state section IDs differ; missing in HTML={sorted(registered - discovered)}, "
            f"unregistered in HTML={sorted(discovered - registered)}"
        )
    for section_id, entry in state["sections"].items():
        actual_slides = list(evidence[section_id].slide_ids)
        if entry["slideIds"] != actual_slides:
            raise WorkflowError(
                f"Section {section_id!r} slideIds differ from HTML: "
                f"state={entry['slideIds']}, HTML={actual_slides}"
            )
        if entry["status"] == "approved" and entry["approvalDigest"] != evidence[section_id].digest:
            raise WorkflowError(
                f"Approved section changed after approval: {section_id}; unlock and review it again"
            )
        if require_approved and entry["status"] != "approved":
            raise WorkflowError(f"Section is not approved: {section_id}")
    return evidence


def validate_html_authoring(root: Path, state: dict[str, Any], *, require_approved: bool = False) -> None:
    if state.get("schemaVersion") == 2 and state["authoring"]["mode"] in {"single", "imported"}:
        validate_sections(root, state, require_approved=require_approved)
    else:
        validate_chapters(root, state, require_complete=require_approved)


def _load_sidecar(path: Path) -> dict[str, Any]:
    return _read_json(path, label="Bento JSON sidecar")


def _atomic_write_bento_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (serialize_bento_doc(document) + "\n").encode("utf-8")
    handle = tempfile.NamedTemporaryFile(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False)
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def initialize_final_baseline(root: Path, state: dict[str, Any], document: dict[str, Any]) -> None:
    if state["validation"].get("finalBaseline") is not None:
        return
    path = _final_baseline_path(root, state)
    _atomic_write_bento_json(path, document)
    state["validation"]["finalBaseline"] = {
        "path": path.relative_to(root).as_posix(),
        "documentRevision": document_revision(document),
        "protectedContentFingerprint": protected_content_fingerprint(document),
    }


def _baseline_document(
    root: Path,
    state: dict[str, Any],
    generated_document: dict[str, Any],
    *,
    allow_missing: bool,
) -> tuple[dict[str, Any], str]:
    metadata = state["validation"].get("finalBaseline")
    if metadata is None:
        if not allow_missing:
            raise WorkflowError("Final content baseline has not been initialized")
        return generated_document, protected_content_fingerprint(generated_document)
    path_field = "documentPath" if state.get("schemaVersion") == 2 else "path"
    path = _repo_path(root, metadata[path_field], field=f"validation.finalBaseline.{path_field}")
    if not path.is_file():
        raise WorkflowError(f"Final content baseline does not exist: {metadata[path_field]}")
    document = _load_sidecar(path)
    if document_revision(document) != metadata["documentRevision"]:
        raise WorkflowError("Final content baseline document revision does not match deck.yaml")
    fingerprint = protected_content_fingerprint(document)
    if fingerprint != metadata["protectedContentFingerprint"]:
        raise WorkflowError("Final content baseline fingerprint does not match deck.yaml")
    return document, fingerprint


def load_final_baseline(
    root: Path, state: dict[str, Any], generated_document: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """Load and verify the immutable finalization baseline recorded in deck.yaml."""

    return _baseline_document(root, state, generated_document, allow_missing=False)


def validate_output_bundle(
    root: Path,
    state: dict[str, Any],
    *,
    require_final: bool,
    allow_missing_baseline: bool = False,
) -> dict[str, Any]:
    outputs = state["outputs"]
    generated_html_path = _repo_path(root, outputs["generatedHtml"], field="outputs.generatedHtml")
    generated_json_path = _repo_path(root, outputs["generatedJson"], field="outputs.generatedJson")
    if not generated_html_path.is_file() or not generated_json_path.is_file():
        raise WorkflowError("Generated Bento HTML and JSON sidecar must both exist")
    generated_html = load_html(generated_html_path)
    generated_document = extract_bento_doc(generated_html)
    validate_bento_doc(generated_document)
    if generated_document != _load_sidecar(generated_json_path):
        raise WorkflowError("Generated Bento HTML #bento-doc and JSON sidecar differ")

    output_root = generated_html_path.parent
    registry_output = (
        _repo_path(root, outputs["generatedRegistry"], field="outputs.generatedRegistry")
        if state.get("schemaVersion") == 2 else output_root / "diagnostics/merged-registry.json"
    )
    required = [
        output_root / "conversion-report.json",
        output_root / "diagnostics/computed-layout.json",
        registry_output,
        output_root / "diagnostics/resource-scan.json",
        output_root / "diagnostics/browser-check.json",
    ]
    missing = [str(path.relative_to(root)) for path in required if not path.is_file()]
    if missing:
        raise WorkflowError("Required conversion diagnostics are missing: " + ", ".join(missing))
    report = _read_json(required[0], label="conversion report")
    resource_scan = _read_json(required[3], label="resource scan")
    browser_check = _read_json(required[4], label="browser check")
    summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
    if summary.get("criticalElementFail", 0) != 0:
        raise WorkflowError("Conversion report contains critical visual failures")
    if summary.get("unresolvedLocalResourceReferences", 0) != 0:
        raise WorkflowError("Conversion report contains unresolved local resources")
    if resource_scan.get("passed") is not True or resource_scan.get("unresolved"):
        raise WorkflowError("Recursive resource scan did not pass")
    if browser_check.get("serialize_roundtrip") is not True:
        raise WorkflowError("Bento browser serialize round-trip did not pass")

    result = {
        "generatedDocument": generated_document,
        "generatedRuntime": runtime_fingerprint(generated_html),
        "registry": required[2],
    }
    if not require_final:
        return result

    final_html_path = _repo_path(root, outputs["finalHtml"], field="outputs.finalHtml")
    final_json_path = _repo_path(root, outputs["finalJson"], field="outputs.finalJson")
    if not final_html_path.is_file() or not final_json_path.is_file():
        raise WorkflowError("Final Bento HTML and JSON sidecar must both exist")
    final_html = load_html(final_html_path)
    final_document = extract_bento_doc(final_html)
    if final_document != _load_sidecar(final_json_path):
        raise WorkflowError("Final Bento HTML #bento-doc and JSON sidecar differ")
    if state.get("schemaVersion") == 2:
        final_registry_value = outputs.get("finalRegistry")
        if not isinstance(final_registry_value, str):
            raise WorkflowError("Final registry path is unavailable")
        final_registry_path = _repo_path(root, final_registry_value, field="outputs.finalRegistry")
        if not final_registry_path.is_file():
            raise WorkflowError("Final Bento registry must exist")
        registry = _read_json(final_registry_path, label="final registry")
        validate_registry(registry, allow_v1=False)
    else:
        registry = _read_json(required[2], label="merged registry")
    baseline_document, baseline_fingerprint = _baseline_document(
        root, state, generated_document, allow_missing=allow_missing_baseline,
    )
    if state.get("schemaVersion") == 2:
        baseline = state["validation"].get("finalBaseline")
        if not isinstance(baseline, dict):
            raise WorkflowError("Final registry baseline has not been initialized")
        baseline_registry_path = _repo_path(
            root, baseline["registryPath"], field="validation.finalBaseline.registryPath",
        )
        if not baseline_registry_path.is_file():
            raise WorkflowError("Final registry baseline does not exist")
        baseline_registry = _read_json(baseline_registry_path, label="final registry baseline")
        validate_registry(baseline_registry, allow_v1=False)
        if registry_revision(baseline_registry) != baseline["registryRevision"]:
            raise WorkflowError("Final registry baseline revision does not match deck.yaml")
        if registry != baseline_registry:
            raise WorkflowError("Final registry changed after content approval")
    validate_editor_document(final_document, current=baseline_document, registry=registry, allow_content_edit=False)
    if protected_content_fingerprint(final_document) != baseline_fingerprint:
        raise WorkflowError(
            "Final Bento content/structure differs from its finalization baseline; only presentation edits are allowed"
        )
    if runtime_fingerprint(final_html) != result["generatedRuntime"]:
        raise WorkflowError("Final Bento runtime differs from generated runtime")
    result["finalDocument"] = final_document
    return result


def authoring_storage(root: Path, state: dict[str, Any]) -> AuthoringArtifactStorage:
    if state.get("schemaVersion") != 2:
        raise WorkflowError("Bento authoring storage requires deck schema v2")
    outputs = state["outputs"]
    if any(outputs[field] is None for field in ("authoringHtml", "authoringJson", "authoringRegistry")):
        raise WorkflowError("Migrated late-stage decks do not have authoring artifacts")
    storage = AuthoringArtifactStorage(
        source=_repo_path(root, outputs["generatedHtml"], field="outputs.generatedHtml"),
        source_registry=_repo_path(root, outputs["generatedRegistry"], field="outputs.generatedRegistry"),
        target=_repo_path(root, outputs["authoringHtml"], field="outputs.authoringHtml"),
        target_registry=_repo_path(root, outputs["authoringRegistry"], field="outputs.authoringRegistry"),
        repository=root, state_path=root / STATE_RELATIVE,
    )
    expected_sidecar = _repo_path(root, outputs["authoringJson"], field="outputs.authoringJson")
    if storage.sidecar != expected_sidecar:
        raise WorkflowError("Authoring storage sidecar differs from outputs.authoringJson")
    return storage


def _source_type(path: str) -> str:
    suffix = Path(path).suffix.lower()
    return {
        ".pdf": "paper", ".md": "document", ".markdown": "document",
        ".txt": "document", ".html": "html", ".htm": "html",
        ".json": "dataset", ".csv": "dataset", ".tsv": "dataset",
        ".png": "image", ".jpg": "image", ".jpeg": "image", ".svg": "image",
    }.get(suffix, "document")


def _migration_manifest(state: dict[str, Any]) -> dict[str, Any]:
    items: list[dict[str, str]] = []
    used: set[str] = set()

    def add(path: str, role: str, preferred_id: str) -> None:
        source_id = preferred_id
        suffix = 2
        while source_id in used:
            source_id = f"{preferred_id}-{suffix}"
            suffix += 1
        used.add(source_id)
        items.append({"id": source_id, "path": path, "type": _source_type(path), "role": role})

    primary = state["project"].get("primarySource")
    if primary:
        add(primary, "primary", "primary-source")
    for index, path in enumerate(state["project"].get("supplementarySources", []), start=1):
        add(path, "supplementary", f"supplementary-{index}")
    return {"schemaVersion": 1, "authorityMode": "single", "items": items}


def _migration_sections(state: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for chapter_id, chapter in state["chapters"].items():
        approved = chapter["status"] == "complete" and chapter["visualApproval"] == "approved"
        status = "approved" if approved else chapter["status"]
        if status == "complete":
            status = "review"
        result[chapter_id] = {
            "title": chapter_id,
            "status": status,
            "slideIds": [],
            "approvalDigest": None,
        }
    return result


def _migrated_stage_source(stage: str) -> str:
    return STAGE_SOURCE[stage]


def _migration_registry_snapshot(
    root: Path, state: dict[str, Any], source_manifest: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    stage = state["workflow"]["stage"]
    previous_stage = state["workflow"].get("blockedFrom", {}).get("stage") if stage == "blocked" else None
    needs_snapshot = stage in {"bento_validation", "bento_finalization", "complete"} or previous_stage in {
        "bento_validation", "bento_finalization",
    }
    if not needs_snapshot:
        return None, None
    generated_html = _repo_path(root, state["outputs"]["generatedHtml"], field="outputs.generatedHtml")
    registry_path = generated_html.parent / "diagnostics" / "merged-registry.json"
    if not registry_path.is_file():
        raise WorkflowError(f"Late-stage migration requires merged registry: {registry_path}")
    try:
        registry = load_registry(registry_path)
        normalized = normalize_registry(registry, unit_id="deck", source_manifest=source_manifest)
        validate_registry(normalized, allow_v1=False)
    except BentoConverterError as exc:
        raise WorkflowError(f"Late-stage merged registry validation failed: {exc}") from exc
    return normalized, registry_revision(normalized)


def _validate_v1_late_artifacts(root: Path, state: dict[str, Any]) -> None:
    stage = state["workflow"]["stage"]
    previous_stage = state["workflow"].get("blockedFrom", {}).get("stage") if stage == "blocked" else None
    if stage not in {"bento_validation", "bento_finalization", "complete"} and previous_stage not in {
        "bento_validation", "bento_finalization",
    }:
        return
    outputs = state["outputs"]
    final_html_path = _repo_path(root, outputs["finalHtml"], field="outputs.finalHtml")
    final_json_path = _repo_path(root, outputs["finalJson"], field="outputs.finalJson")
    if not final_html_path.is_file() or not final_json_path.is_file():
        raise WorkflowError("Late-stage migration requires the existing final HTML/JSON pair")
    final_document = extract_bento_doc(load_html(final_html_path))
    if final_document != _load_sidecar(final_json_path):
        raise WorkflowError("Late-stage migration final HTML and JSON sidecar differ")
    validate_bento_doc(final_document)
    baseline = state["validation"].get("finalBaseline")
    if not isinstance(baseline, dict):
        raise WorkflowError("Late-stage migration requires the existing final baseline")
    baseline_path = _repo_path(root, baseline["path"], field="validation.finalBaseline.path")
    if not baseline_path.is_file():
        raise WorkflowError(f"Late-stage migration baseline does not exist: {baseline['path']}")
    baseline_document = _load_sidecar(baseline_path)
    if document_revision(baseline_document) != baseline["documentRevision"]:
        raise WorkflowError("Late-stage migration baseline revision does not match deck.yaml")
    if protected_content_fingerprint(baseline_document) != baseline["protectedContentFingerprint"]:
        raise WorkflowError("Late-stage migration baseline fingerprint does not match deck.yaml")


def migrate_v1_state(
    root: Path, state: dict[str, Any], *, dry_run: bool,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if state.get("schemaVersion") == 2:
        report = {
            "format": "bento/deck-migration-report/v1", "changed": False,
            "fromSchemaVersion": 2, "toSchemaVersion": 2, "dryRun": dry_run,
        }
        return copy.deepcopy(state), report, load_source_manifest(root, state, require_exists=False)
    if state.get("schemaVersion") != 1:
        raise WorkflowError("Only deck schema v1 can be migrated")

    manifest = _migration_manifest(state)
    manifest_path = root / "sources/source-manifest.yaml"
    if manifest_path.exists():
        try:
            existing_manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            raise WorkflowError(f"Cannot validate existing source manifest before migration: {exc}") from exc
        if existing_manifest != manifest:
            raise WorkflowError("Existing sources/source-manifest.yaml differs from the v1 migration result")
    _validate_v1_late_artifacts(root, state)
    _registry, registry_revision_value = _migration_registry_snapshot(root, state, manifest)
    now = utc_now()
    stage = state["workflow"]["stage"]
    blocked_from = copy.deepcopy(state["workflow"].get("blockedFrom"))
    late_stage = stage in {"bento_finalization", "complete"} or (
        stage == "blocked" and isinstance(blocked_from, dict) and blocked_from["stage"] == "bento_finalization"
    )
    outputs = state["outputs"]
    final_html = Path(outputs["finalHtml"])
    final_registry = final_html.with_name(
        final_html.name[: -len(".bento.html")] + ".registry.json"
        if final_html.name.endswith(".bento.html") else final_html.stem + ".registry.json"
    ).as_posix()
    migrated_workflow = copy.deepcopy(state["workflow"])
    migrated_workflow["sourceOfTruth"] = (
        _migrated_stage_source(blocked_from["stage"]) if stage == "blocked"
        else _migrated_stage_source(stage)
    )
    migrated_workflow["currentSection"] = migrated_workflow.get("currentChapter")
    if blocked_from is not None:
        blocked_from["sourceOfTruth"] = _migrated_stage_source(blocked_from["stage"])
        blocked_from["currentSection"] = blocked_from.get("currentChapter")
        migrated_workflow["blockedFrom"] = blocked_from
    baseline = state["validation"].get("finalBaseline")
    migrated_baseline = None
    if baseline is not None:
        if registry_revision_value is None:
            raise WorkflowError("A v1 final baseline cannot migrate without a validated merged registry")
        migrated_baseline = {
            "documentPath": baseline["path"],
            "documentRevision": baseline["documentRevision"],
            "registryPath": _final_registry_baseline_path(root, {
                "outputs": {"finalHtml": outputs["finalHtml"]}
            }).relative_to(root).as_posix(),
            "registryRevision": registry_revision_value,
            "protectedContentFingerprint": baseline["protectedContentFingerprint"],
        }
    approved_content = late_stage and baseline is not None and registry_revision_value is not None
    bento_content = {
        "status": "approved" if approved_content else "pending",
        "documentRevision": baseline["documentRevision"] if approved_content else None,
        "registryRevision": registry_revision_value if approved_content else None,
        "approvalDigest": (
            content_approval_digest(baseline["documentRevision"], registry_revision_value)
            if approved_content else None
        ),
        "approvedAt": now if approved_content else None,
    }
    migrated = {
        "schemaVersion": 2,
        "project": {
            "kind": "paper_explanation",
            "title": state["project"]["title"],
            "request": state["project"]["request"],
            "primarySource": state["project"]["primarySource"],
            "supplementarySources": state["project"]["supplementarySources"],
        },
        "sources": {"manifest": "sources/source-manifest.yaml", "authorityMode": manifest["authorityMode"]},
        "authoring": {
            "mode": "modular", "entryHtml": None, "registry": None,
            "currentSection": migrated_workflow["currentSection"],
        },
        "workflow": migrated_workflow,
        "approvals": {
            **state["approvals"],
            "bentoContent": bento_content,
        },
        "sections": _migration_sections(state),
        "chapters": copy.deepcopy(state["chapters"]),
        "handoff": {
            "readyForCodex": state["handoff"]["readyForCodex"],
            "readyForBentoAuthoring": stage == "bento_authoring",
            "readyForContentReview": stage == "content_review",
            "readyForFinalEditing": state["handoff"]["readyForFinalEditing"],
        },
        "outputs": {
            "generatedHtml": outputs["generatedHtml"],
            "generatedJson": outputs["generatedJson"],
            "generatedRegistry": str(Path(outputs["generatedHtml"]).parent / "diagnostics/merged-registry.json").replace("\\", "/"),
            "authoringHtml": None if late_stage else "output/presentation.authoring.bento.html",
            "authoringJson": None if late_stage else "output/presentation.authoring.bento.json",
            "authoringRegistry": None if late_stage else "output/presentation.authoring.registry.json",
            "finalHtml": outputs["finalHtml"],
            "finalJson": outputs["finalJson"],
            "finalRegistry": final_registry,
        },
        "preview": copy.deepcopy(state["preview"]),
        "validation": {
            "finalStatus": state["validation"]["finalStatus"],
            "checkedAt": state["validation"]["checkedAt"],
            "finalBaseline": migrated_baseline,
        },
        "migration": {
            "fromSchemaVersion": 1,
            "migratedAt": now,
            "lateStageCompatibility": late_stage,
        },
    }
    validate_state(root, migrated)
    report = {
        "format": "bento/deck-migration-report/v1",
        "changed": True,
        "fromSchemaVersion": 1,
        "toSchemaVersion": 2,
        "dryRun": dry_run,
        "stage": stage,
        "authoringMode": "modular",
        "lateStageCompatibility": late_stage,
        "sourceManifestItems": len(manifest["items"]),
        "registryRevision": registry_revision_value,
    }
    return migrated, report, manifest


def command_migrate(
    root: Path, state: dict[str, Any], *, dry_run: bool, report_path: Path | None,
) -> None:
    try:
        migrated, report, manifest = migrate_v1_state(root, state, dry_run=dry_run)
    except WorkflowError as exc:
        failure_report = {
            "format": "bento/deck-migration-report/v1", "changed": False,
            "fromSchemaVersion": state.get("schemaVersion"), "toSchemaVersion": 2,
            "dryRun": dry_run, "status": "failed", "reasons": [str(exc)],
        }
        if not dry_run:
            _atomic_write_json(report_path or (root / "output/migration-report.json"), failure_report)
        raise
    if dry_run or not report["changed"]:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    original_state = (root / STATE_RELATIVE).read_bytes()
    backup = root / "deck.v1.backup.yaml"
    if backup.exists() and backup.read_bytes() != original_state:
        raise WorkflowError(f"Migration backup already exists with different content: {backup}")
    manifest_path = _repo_path(root, migrated["sources"]["manifest"], field="sources.manifest")
    manifest_payload = yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False).encode("utf-8")
    destination = report_path or (root / "output/migration-report.json")
    payloads: dict[Path, bytes] = {
        root / STATE_RELATIVE: yaml.safe_dump(migrated, allow_unicode=True, sort_keys=False).encode("utf-8"),
    }
    if not backup.exists():
        payloads[backup] = original_state
    if not manifest_path.exists():
        payloads[manifest_path] = manifest_payload
    normalized_registry, _ = _migration_registry_snapshot(root, state, manifest)
    if normalized_registry is not None:
        registry_payload = (
            json.dumps(normalized_registry, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        ).encode("utf-8")
        final_registry = _repo_path(root, migrated["outputs"]["finalRegistry"], field="outputs.finalRegistry")
        payloads[final_registry] = registry_payload
        baseline = migrated["validation"]["finalBaseline"]
        if baseline is not None:
            baseline_registry = _repo_path(root, baseline["registryPath"], field="validation.finalBaseline.registryPath")
            payloads[baseline_registry] = registry_payload
    transaction = ArtifactTransactionStore(root, payloads)

    def validate_migration_commit() -> None:
        installed_state = yaml.safe_load((root / STATE_RELATIVE).read_text(encoding="utf-8-sig"))
        if not isinstance(installed_state, dict):
            raise WorkflowError("Migrated deck.yaml root is not a mapping")
        validate_state(root, installed_state)
        if normalized_registry is not None:
            installed_final_registry = load_registry(
                _repo_path(root, installed_state["outputs"]["finalRegistry"], field="outputs.finalRegistry")
            )
            if installed_final_registry != normalized_registry:
                raise WorkflowError("Migrated final registry snapshot differs from the validated registry")
            baseline_metadata = installed_state["validation"]["finalBaseline"]
            if baseline_metadata is not None:
                installed_baseline_registry = load_registry(
                    _repo_path(root, baseline_metadata["registryPath"], field="validation.finalBaseline.registryPath")
                )
                if installed_baseline_registry != normalized_registry:
                    raise WorkflowError("Migrated registry baseline differs from the validated registry")
                if registry_revision(installed_baseline_registry) != baseline_metadata["registryRevision"]:
                    raise WorkflowError("Migrated registry baseline revision differs from deck.yaml")

    transaction.commit(
        payloads,
        operation="schema-v1-to-v2-migration",
        target_registry_revision=report.get("registryRevision"),
        report_path=destination,
        report_payload=report,
        validate_committed=validate_migration_commit,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


def command_status(root: Path, state: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        # ASCII-safe JSON survives Windows PowerShell 5 native-process decoding;
        # ConvertFrom-Json restores the original Unicode path strings.
        print(json.dumps(state, ensure_ascii=True, indent=2))
        return
    workflow = state["workflow"]
    print(f"stage: {workflow['stage']}")
    print(f"status: {workflow['status']}")
    print(f"owner: {workflow['owner']}")
    print(f"source of truth: {workflow['sourceOfTruth']}")
    print(f"current chapter: {workflow['currentChapter'] or '-'}")
    if state.get("schemaVersion") == 2:
        print(f"current section: {workflow['currentSection'] or '-'}")
    if workflow.get("blockingReason"):
        print(f"blocking reason: {workflow['blockingReason']}")


def command_initialize(root: Path, state: dict[str, Any]) -> None:
    _require_stage(state, "initialized")
    selected, _ = discover_source_candidates(root, state)
    state["project"]["primarySource"] = selected.relative_to(root).as_posix() if selected else None
    _transition(state, "planning", "in_progress")
    atomic_write_state(root, state)
    append_work_log(root, f"Initialized planning with primary source {state['project']['primarySource']}")


def command_configure_chapters(root: Path, state: dict[str, Any], chapter_ids: Iterable[str]) -> None:
    _require_stage(state, "planning", "awaiting_plan_approval")
    if state.get("schemaVersion") == 2 and state["authoring"]["mode"] != "modular":
        raise WorkflowError("configure-chapters is only available in modular authoring mode")
    values = list(dict.fromkeys(chapter_ids))
    if not values or any(not CHAPTER_PATTERN.fullmatch(value) for value in values):
        raise WorkflowError("Chapter IDs must use chapter-XX names")
    for existing, entry in state["chapters"].items():
        if existing not in values and entry["status"] != "planned":
            raise WorkflowError(f"Cannot remove chapter after authoring has begun: {existing}")
    state["chapters"] = {
        chapter_id: state["chapters"].get(chapter_id, {
            "html": f"chapters/{chapter_id}.preview.html",
            "registry": f"chapters/{chapter_id}.registry.json",
            "status": "planned",
            "visualApproval": "pending",
        })
        for chapter_id in values
    }
    atomic_write_state(root, state)
    append_work_log(root, "Configured chapters: " + ", ".join(values))


def command_configure_sections(root: Path, state: dict[str, Any], section_ids: Iterable[str]) -> None:
    _require_stage(state, "planning", "awaiting_plan_approval")
    if state.get("schemaVersion") != 2 or state["authoring"]["mode"] not in {"single", "imported"}:
        raise WorkflowError("configure-sections requires schema v2 single/imported authoring")
    values = list(dict.fromkeys(section_ids))
    if not values or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value) for value in values):
        raise WorkflowError("Section IDs must use stable alphanumeric, dot, underscore, or hyphen names")
    for existing, entry in state["sections"].items():
        if existing not in values and entry["status"] != "planned":
            raise WorkflowError(f"Cannot remove section after authoring has begun: {existing}")
    state["sections"] = {
        section_id: state["sections"].get(section_id, {
            "title": section_id,
            "status": "planned",
            "slideIds": [],
            "approvalDigest": None,
        })
        for section_id in values
    }
    atomic_write_state(root, state)
    append_work_log(root, "Configured sections: " + ", ".join(values))


def command_submit_plan(root: Path, state: dict[str, Any]) -> None:
    _require_stage(state, "planning")
    validate_planning(root)
    planned_units = state["sections"] if state.get("schemaVersion") == 2 and state["authoring"]["mode"] != "modular" else state["chapters"]
    if not planned_units:
        raise WorkflowError("Register the planned sections or chapters before requesting approval")
    _transition(state, "awaiting_plan_approval", "awaiting_approval")
    atomic_write_state(root, state)
    append_work_log(root, "Submitted explanation policy, story outline, and slide plan for approval")


def command_approve_plan(root: Path, state: dict[str, Any]) -> None:
    _require_stage(state, "awaiting_plan_approval")
    validate_planning(root)
    single = state.get("schemaVersion") == 2 and state["authoring"]["mode"] != "modular"
    planned_units = state["sections"] if single else state["chapters"]
    if not planned_units:
        raise WorkflowError("No sections or chapters are configured")
    for key in ("explanationPolicy", "storyOutline", "slidePlan"):
        state["approvals"][key] = "approved"
    first = next(iter(planned_units))
    _transition(state, "html_authoring", "in_progress", current=first)
    atomic_write_state(root, state)
    append_work_log(root, "Recorded plan approval and opened HTML authoring")


def _select_section(state: dict[str, Any], requested: str | None) -> str:
    if requested:
        if requested not in state["sections"]:
            raise WorkflowError(f"Section is not registered: {requested}")
        return requested
    current = state["workflow"].get("currentSection")
    if current and state["sections"][current]["status"] != "approved":
        return current
    for section_id, entry in state["sections"].items():
        if entry["status"] != "approved":
            return section_id
    raise WorkflowError("All registered sections are approved")


def command_begin_section(root: Path, state: dict[str, Any], requested: str | None) -> None:
    _require_stage(state, "html_authoring")
    section_id = _select_section(state, requested)
    entry = state["sections"][section_id]
    if entry["status"] not in {"planned", "authoring"}:
        raise WorkflowError(f"Section cannot enter authoring from status {entry['status']!r}: {section_id}")
    entry["status"] = "authoring"
    entry["approvalDigest"] = None
    _transition(state, "html_authoring", "in_progress", current=section_id)
    atomic_write_state(root, state)
    append_work_log(root, f"Began authoring section {section_id}")


def command_complete_section(root: Path, state: dict[str, Any], requested: str | None) -> None:
    _require_stage(state, "html_authoring")
    section_id = _select_section(state, requested)
    if state["sections"][section_id]["status"] != "authoring":
        raise WorkflowError(f"Section is not in authoring: {section_id}")
    evidence = load_single_section_evidence(root, state)
    if set(evidence) != set(state["sections"]):
        raise WorkflowError("Single HTML section IDs must exactly match deck.yaml before review")
    current = evidence[section_id]
    entry = state["sections"][section_id]
    entry["slideIds"] = list(current.slide_ids)
    entry["status"] = "review"
    entry["approvalDigest"] = None
    _transition(state, "html_review", "awaiting_approval", current=section_id)
    atomic_write_state(root, state)
    append_work_log(root, f"Validated section {section_id} and requested visual approval")


def command_approve_section(root: Path, state: dict[str, Any], requested: str | None) -> None:
    _require_stage(state, "html_review")
    section_id = requested or state["workflow"].get("currentSection")
    if not section_id or section_id not in state["sections"]:
        raise WorkflowError("No current section is available for visual approval")
    entry = state["sections"][section_id]
    if entry["status"] != "review":
        raise WorkflowError(f"Section is not awaiting visual approval: {section_id}")
    evidence = load_single_section_evidence(root, state)
    if set(evidence) != set(state["sections"]):
        raise WorkflowError("Single HTML section IDs must exactly match deck.yaml before approval")
    current = evidence[section_id]
    if entry["slideIds"] != list(current.slide_ids):
        raise WorkflowError(f"Section slide membership changed during review: {section_id}")
    # Revalidate every previously approved section. A global CSS/theme edit will
    # change every digest and cannot silently ride along with one section review.
    for approved_id, approved in state["sections"].items():
        if approved["status"] == "approved" and approved["approvalDigest"] != evidence[approved_id].digest:
            raise WorkflowError(f"Approved section changed after approval: {approved_id}; unlock it first")
    entry["status"] = "approved"
    entry["approvalDigest"] = current.digest
    remaining = [key for key, value in state["sections"].items() if value["status"] != "approved"]
    if remaining:
        next_section = remaining[0]
        state["sections"][next_section]["status"] = "authoring"
        _transition(state, "html_authoring", "in_progress", current=next_section)
    else:
        validate_sections(root, state, require_approved=True)
        state["handoff"]["readyForCodex"] = True
        _transition(state, "ready_for_conversion", "ready")
    atomic_write_state(root, state)
    append_work_log(root, f"Approved visual composition for section {section_id}")


def command_unlock_section(root: Path, state: dict[str, Any], section_id: str) -> None:
    _require_stage(state, "html_authoring", "html_review", "ready_for_conversion")
    if section_id not in state["sections"]:
        raise WorkflowError(f"Section is not registered: {section_id}")
    entry = state["sections"][section_id]
    if entry["status"] != "approved":
        raise WorkflowError(f"Section is not approved: {section_id}")
    entry["status"] = "authoring"
    entry["approvalDigest"] = None
    state["handoff"]["readyForCodex"] = False
    _transition(state, "html_authoring", "in_progress", current=section_id)
    atomic_write_state(root, state)
    append_work_log(root, f"Unlocked section {section_id} for HTML authoring")


def _select_chapter(state: dict[str, Any], requested: str | None) -> str:
    if requested:
        if requested not in state["chapters"]:
            raise WorkflowError(f"Chapter is not registered: {requested}")
        return requested
    current = state["workflow"].get("currentChapter")
    if current and state["chapters"][current]["status"] != "complete":
        return current
    for chapter_id, entry in state["chapters"].items():
        if entry["status"] != "complete":
            return chapter_id
    raise WorkflowError("All registered chapters are complete")


def command_begin_chapter(root: Path, state: dict[str, Any], requested: str | None) -> None:
    _require_stage(state, "html_authoring")
    chapter_id = _select_chapter(state, requested)
    entry = state["chapters"][chapter_id]
    if entry["status"] not in {"planned", "authoring"}:
        raise WorkflowError(f"Chapter cannot enter authoring from status {entry['status']!r}: {chapter_id}")
    entry["status"] = "authoring"
    _transition(state, "html_authoring", "in_progress", current=chapter_id)
    atomic_write_state(root, state)
    append_work_log(root, f"Began authoring {chapter_id}")


def command_complete_chapter(root: Path, state: dict[str, Any], requested: str | None) -> None:
    _require_stage(state, "html_authoring")
    chapter_id = _select_chapter(state, requested)
    entry = state["chapters"][chapter_id]
    _load_chapter(root, chapter_id, entry)
    entry["status"] = "review"
    entry["visualApproval"] = "pending"
    _transition(state, "html_review", "awaiting_approval", current=chapter_id)
    atomic_write_state(root, state)
    append_work_log(root, f"Validated {chapter_id} and requested visual approval")


def command_approve_chapter(root: Path, state: dict[str, Any], requested: str | None) -> None:
    _require_stage(state, "html_review")
    chapter_id = requested or state["workflow"].get("currentChapter")
    if not chapter_id or chapter_id not in state["chapters"]:
        raise WorkflowError("No current chapter is available for visual approval")
    entry = state["chapters"][chapter_id]
    if entry["status"] != "review":
        raise WorkflowError(f"Chapter is not awaiting visual approval: {chapter_id}")
    _load_chapter(root, chapter_id, entry)
    entry["status"] = "complete"
    entry["visualApproval"] = "approved"
    remaining = [key for key, value in state["chapters"].items() if value["status"] != "complete"]
    if remaining:
        next_chapter = remaining[0]
        state["chapters"][next_chapter]["status"] = "authoring"
        _transition(state, "html_authoring", "in_progress", current=next_chapter)
    else:
        validate_chapters(root, state, require_complete=True)
        state["handoff"]["readyForCodex"] = True
        _transition(state, "ready_for_conversion", "ready")
    atomic_write_state(root, state)
    append_work_log(root, f"Approved visual composition for {chapter_id}")


def command_prepare_conversion(root: Path, state: dict[str, Any]) -> None:
    _require_stage(state, "ready_for_conversion")
    if any(state["approvals"][key] != "approved" for key in ("explanationPolicy", "storyOutline", "slidePlan")):
        raise WorkflowError("Plan approvals are incomplete")
    validate_html_authoring(root, state, require_approved=True)
    if not state["handoff"]["readyForCodex"]:
        raise WorkflowError("Work-to-Codex handoff is not ready")
    _transition(state, "converting", "in_progress")
    atomic_write_state(root, state)
    append_work_log(root, "Validated conversion readiness and handed the deck to Codex")


def command_mark_converted(root: Path, state: dict[str, Any]) -> None:
    _require_stage(state, "converting")
    bundle = validate_output_bundle(root, state, require_final=False)
    if state.get("schemaVersion") == 2:
        storage = authoring_storage(root, state)
        storage.status()
        state["handoff"]["readyForCodex"] = False
        state["handoff"]["readyForBentoAuthoring"] = True
        state["handoff"]["readyForContentReview"] = False
        state["handoff"]["readyForFinalEditing"] = False
        _transition(state, "bento_validation", "in_progress")
        atomic_write_state(root, state)
        append_work_log(root, "Validated generated output and initialized or retained Bento authoring artifacts")
        return
    outputs = state["outputs"]
    WorkEditorStorage(
        source=_repo_path(root, outputs["generatedHtml"], field="outputs.generatedHtml"),
        target=_repo_path(root, outputs["finalHtml"], field="outputs.finalHtml"),
        registry=bundle["registry"],
        reset_final=False,
        allow_content_edit=False,
    )
    # Before the first handoff, prove that an existing final is still a layout-only
    # descendant of generated. Persist generated as the immutable content baseline.
    validate_output_bundle(root, state, require_final=True, allow_missing_baseline=True)
    initialize_final_baseline(root, state, bundle["generatedDocument"])
    validate_output_bundle(root, state, require_final=True)
    _transition(state, "bento_validation", "in_progress")
    atomic_write_state(root, state)
    append_work_log(root, "Validated generated output and initialized or retained protected final output")


def command_begin_authoring(root: Path, state: dict[str, Any]) -> None:
    if state.get("schemaVersion") != 2:
        raise WorkflowError("Bento authoring is available only after deck schema v2 migration")
    _require_stage(state, "bento_validation")
    validate_output_bundle(root, state, require_final=False)
    storage = authoring_storage(root, state)
    storage.status()
    state["handoff"]["readyForCodex"] = False
    state["handoff"]["readyForBentoAuthoring"] = True
    state["handoff"]["readyForContentReview"] = False
    state["handoff"]["readyForFinalEditing"] = False
    _transition(state, "bento_authoring", "in_progress")
    atomic_write_state(root, state)
    append_work_log(root, "Handed validated generated Bento artifacts to Work for authoring")


def _pending_content_approval() -> dict[str, Any]:
    return {
        "status": "pending", "documentRevision": None, "registryRevision": None,
        "approvalDigest": None, "approvedAt": None,
    }


def _current_authoring_status(root: Path, state: dict[str, Any]) -> dict[str, Any]:
    status = authoring_storage(root, state).status()
    approval = state["approvals"]["bentoContent"]
    if approval["status"] == "approved" and (
        approval["documentRevision"] != status["documentRevision"]
        or approval["registryRevision"] != status["registryRevision"]
    ):
        state["approvals"]["bentoContent"] = _pending_content_approval()
    return status


def command_begin_content_review(root: Path, state: dict[str, Any]) -> None:
    if state.get("schemaVersion") != 2:
        raise WorkflowError("Bento content review requires deck schema v2")
    _require_stage(state, "bento_authoring")
    _current_authoring_status(root, state)
    state["handoff"]["readyForBentoAuthoring"] = False
    state["handoff"]["readyForContentReview"] = True
    state["handoff"]["readyForFinalEditing"] = False
    _transition(state, "content_review", "awaiting_approval")
    atomic_write_state(root, state)
    append_work_log(root, "Validated authoring artifacts and requested Bento content approval")


def command_approve_content(root: Path, state: dict[str, Any]) -> None:
    if state.get("schemaVersion") != 2:
        raise WorkflowError("Bento content approval requires deck schema v2")
    _require_stage(state, "content_review")
    status = _current_authoring_status(root, state)
    document_revision_value = status["documentRevision"]
    registry_revision_value = status["registryRevision"]
    state["approvals"]["bentoContent"] = {
        "status": "approved",
        "documentRevision": document_revision_value,
        "registryRevision": registry_revision_value,
        "approvalDigest": content_approval_digest(document_revision_value, registry_revision_value),
        "approvedAt": utc_now(),
    }
    state["handoff"]["readyForContentReview"] = True
    state["workflow"]["status"] = "ready"
    atomic_write_state(root, state)
    append_work_log(root, "Approved Bento authoring content at fixed document and registry revisions")


def _initialize_v2_finalization(root: Path, state: dict[str, Any]) -> None:
    storage = authoring_storage(root, state)
    storage.acquire_writer_lease()
    try:
        authoring_html, authoring_document, authoring_registry = storage.artifact_snapshot()
        document_revision_value = document_revision(authoring_document)
        registry_revision_value = registry_revision(authoring_registry)
        approval = state["approvals"]["bentoContent"]
        if (
            approval["status"] != "approved"
            or approval["documentRevision"] != document_revision_value
            or approval["registryRevision"] != registry_revision_value
            or approval["approvalDigest"] != content_approval_digest(document_revision_value, registry_revision_value)
        ):
            raise WorkflowError("Current authoring document and registry revisions do not have fresh content approval")
        storage.validate_serialized(
            authoring_html,
            base_document_revision=document_revision_value,
            base_registry_revision=registry_revision_value,
            registry=authoring_registry,
        )
        outputs = state["outputs"]
        final_html = _repo_path(root, outputs["finalHtml"], field="outputs.finalHtml")
        final_json = _repo_path(root, outputs["finalJson"], field="outputs.finalJson")
        final_registry = _repo_path(root, outputs["finalRegistry"], field="outputs.finalRegistry")
        baseline_document = _final_baseline_path(root, state)
        baseline_registry = _final_registry_baseline_path(root, state)
        intended_document_payload = (serialize_bento_doc(authoring_document) + "\n").encode("utf-8")
        intended_registry_payload = (
            json.dumps(authoring_registry, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        ).encode("utf-8")
        payloads = {
            final_html: authoring_html.encode("utf-8"),
            final_json: intended_document_payload,
            final_registry: intended_registry_payload,
            baseline_document: intended_document_payload,
            baseline_registry: intended_registry_payload,
        }
        existing = [path.is_file() for path in payloads]
        if any(existing) and not all(existing):
            raise WorkflowError("Final initialization artifacts are incomplete; recover or remove the incomplete set explicitly")
        if all(existing):
            mismatched = [path for path, payload in payloads.items() if path.read_bytes() != payload]
            if mismatched:
                raise WorkflowError("Existing final artifacts differ from approved authoring content and will not be overwritten")

        next_state = copy.deepcopy(state)
        next_state["validation"]["finalBaseline"] = {
            "documentPath": baseline_document.relative_to(root).as_posix(),
            "documentRevision": document_revision_value,
            "registryPath": baseline_registry.relative_to(root).as_posix(),
            "registryRevision": registry_revision_value,
            "protectedContentFingerprint": protected_content_fingerprint(authoring_document),
        }
        next_state["handoff"]["readyForBentoAuthoring"] = False
        next_state["handoff"]["readyForContentReview"] = False
        next_state["handoff"]["readyForFinalEditing"] = True
        _transition(next_state, "bento_finalization", "in_progress")
        state_payload = yaml.safe_dump(next_state, allow_unicode=True, sort_keys=False).encode("utf-8")
        state_path = root / STATE_RELATIVE
        base_state_payload = state_path.read_bytes()
        transaction_payloads = {**payloads, state_path: state_payload}
        transaction = ArtifactTransactionStore(root, transaction_payloads)

        def validate_base() -> None:
            _, current_document, current_registry = storage.artifact_snapshot()
            if (
                document_revision(current_document) != document_revision_value
                or registry_revision(current_registry) != registry_revision_value
            ):
                raise WorkflowError("Authoring revisions changed before final initialization")
            if state_path.read_bytes() != base_state_payload:
                raise WorkflowError("deck.yaml changed before final initialization")

        def validate_committed() -> None:
            installed_state = yaml.safe_load(state_path.read_text(encoding="utf-8-sig"))
            validate_state(root, installed_state)
            validate_output_bundle(root, installed_state, require_final=True)

        transaction.commit(
            transaction_payloads,
            operation="authoring-to-final-initialize",
            base_document_revision=document_revision_value,
            base_registry_revision=registry_revision_value,
            target_document_revision=document_revision_value,
            target_registry_revision=registry_revision_value,
            validate_base=validate_base,
            validate_committed=validate_committed,
            report_path=final_html.parent / "finalization-initialization-report.json",
            report_payload={
                "operation": "authoring-to-final-initialize",
                "documentRevision": document_revision_value,
                "registryRevision": registry_revision_value,
                "approvalDigest": approval["approvalDigest"],
                "validation": "pass",
            },
        )
        state.clear()
        state.update(next_state)
    finally:
        storage.release_writer_lease()


def command_begin_finalization(root: Path, state: dict[str, Any]) -> None:
    if state.get("schemaVersion") == 2:
        _require_stage(state, "content_review")
        _initialize_v2_finalization(root, state)
        append_work_log(root, "Initialized frozen final artifacts from approved Bento authoring content")
        return
    _require_stage(state, "bento_validation")
    validate_output_bundle(root, state, require_final=True)
    state["handoff"]["readyForCodex"] = False
    state["handoff"]["readyForFinalEditing"] = True
    _transition(state, "bento_finalization", "in_progress")
    atomic_write_state(root, state)
    append_work_log(root, "Handed the validated final Bento artifact to Work for layout finalization")


def command_approve_final(root: Path, state: dict[str, Any]) -> None:
    _require_stage(state, "bento_finalization")
    validate_output_bundle(root, state, require_final=True)
    state["approvals"]["finalBento"] = "approved"
    state["validation"]["finalStatus"] = "pass"
    state["validation"]["checkedAt"] = utc_now()
    state["workflow"]["status"] = "awaiting_approval"
    atomic_write_state(root, state)
    append_work_log(root, "Recorded final Bento approval after technical validation")


def command_complete(root: Path, state: dict[str, Any]) -> None:
    _require_stage(state, "bento_finalization")
    if state["approvals"]["finalBento"] != "approved":
        raise WorkflowError("Final Bento approval is still pending")
    validate_output_bundle(root, state, require_final=True)
    state["validation"]["finalStatus"] = "pass"
    state["validation"]["checkedAt"] = utc_now()
    state["handoff"]["readyForFinalEditing"] = False
    _transition(state, "complete", "complete")
    atomic_write_state(root, state)
    append_work_log(root, "Completed final Bento validation")


def command_block(root: Path, state: dict[str, Any], owner: str, reason: str) -> None:
    if not reason.strip():
        raise WorkflowError("A non-empty blocking reason is required")
    workflow = state["workflow"]
    if workflow["stage"] in {"blocked", "complete"}:
        raise WorkflowError(f"Stage {workflow['stage']!r} cannot be blocked")
    workflow["blockedFrom"] = {
        "stage": workflow["stage"],
        "status": workflow["status"],
        "owner": workflow["owner"],
        "sourceOfTruth": workflow["sourceOfTruth"],
        "currentChapter": workflow["currentChapter"],
    }
    if state.get("schemaVersion") == 2:
        workflow["blockedFrom"]["currentSection"] = workflow["currentSection"]
    workflow.update({"stage": "blocked", "status": "blocked", "owner": owner, "blockingReason": reason})
    atomic_write_state(root, state)
    append_work_log(root, f"Blocked ({owner}): {reason}")


def _validate_resume_target(root: Path, state: dict[str, Any], snapshot: dict[str, Any]) -> None:
    stage = snapshot["stage"]
    if stage == "initialized":
        return
    if stage in {"planning", "awaiting_plan_approval"}:
        discover_source_candidates(root, state)
        if stage == "awaiting_plan_approval":
            validate_planning(root)
            units = state["sections"] if state.get("schemaVersion") == 2 and state["authoring"]["mode"] != "modular" else state["chapters"]
            if not units:
                raise WorkflowError("Cannot resume plan approval without configured sections or chapters")
        return
    if any(state["approvals"][key] != "approved" for key in ("explanationPolicy", "storyOutline", "slidePlan")):
        raise WorkflowError(f"Cannot resume {stage!r} while plan approvals are incomplete")
    single = state.get("schemaVersion") == 2 and state["authoring"]["mode"] != "modular"
    units = state["sections"] if single else state["chapters"]
    if not units:
        raise WorkflowError(f"Cannot resume {stage!r} without configured sections or chapters")
    if stage == "html_authoring":
        return
    if stage == "html_review":
        current = snapshot["currentSection"] if single else snapshot["currentChapter"]
        if not current:
            raise WorkflowError("Cannot resume HTML review without a current section or chapter")
        entry = units[current]
        if entry["status"] != "review":
            raise WorkflowError(f"Authoring unit is not awaiting review: {current}")
        if single:
            evidence = load_single_section_evidence(root, state)
            if current not in evidence or list(evidence[current].slide_ids) != entry["slideIds"]:
                raise WorkflowError(f"Section changed while workflow was blocked: {current}")
        else:
            _load_chapter(root, current, entry)
        return
    if stage in {"ready_for_conversion", "converting"}:
        validate_html_authoring(root, state, require_approved=True)
        if not state["handoff"]["readyForCodex"]:
            raise WorkflowError("Cannot resume conversion because the Work-to-Codex handoff is not ready")
        return
    if stage == "bento_validation":
        if state.get("schemaVersion") == 2:
            validate_output_bundle(root, state, require_final=False)
            authoring_storage(root, state).status()
        else:
            validate_output_bundle(root, state, require_final=True)
        return
    if stage in {"bento_authoring", "content_review"}:
        if state.get("schemaVersion") != 2:
            raise WorkflowError(f"Stage {stage!r} requires deck schema v2")
        authoring_storage(root, state).status()
        return
    if stage == "bento_finalization":
        validate_output_bundle(root, state, require_final=True)
        if not state["handoff"]["readyForFinalEditing"]:
            raise WorkflowError("Cannot resume finalization because the Codex-to-Work handoff is not ready")
        return
    raise WorkflowError(f"Unsupported resume target: {stage}")


def command_resume(root: Path, state: dict[str, Any]) -> None:
    _require_stage(state, "blocked")
    snapshot = state["workflow"].get("blockedFrom")
    if not isinstance(snapshot, dict):
        raise WorkflowError("Blocked state has no resumable workflow snapshot")
    _validate_resume_target(root, state, snapshot)
    state["workflow"].update(snapshot)
    state["workflow"]["blockingReason"] = None
    state["workflow"]["blockedFrom"] = None
    atomic_write_state(root, state)
    append_work_log(root, f"Resumed workflow at {snapshot['stage']}")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--root", type=Path, help="Repository root (defaults to the checkout containing this module)")
    commands = result.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status")
    status.add_argument("--json", action="store_true", dest="as_json")
    commands.add_parser("validate")
    migrate = commands.add_parser("migrate")
    migrate.add_argument("--dry-run", action="store_true")
    migrate.add_argument("--report", type=Path)
    commands.add_parser("initialize")
    configure = commands.add_parser("configure-chapters")
    configure.add_argument("chapters", nargs="+")
    configure_sections = commands.add_parser("configure-sections")
    configure_sections.add_argument("sections", nargs="+")
    commands.add_parser("submit-plan")
    commands.add_parser("approve-plan")
    for name in ("begin-chapter", "complete-chapter", "approve-chapter"):
        child = commands.add_parser(name)
        child.add_argument("--chapter")
    for name in ("begin-section", "complete-section", "approve-section"):
        child = commands.add_parser(name)
        child.add_argument("--section")
    unlock = commands.add_parser("unlock-section")
    unlock.add_argument("--section", required=True)
    commands.add_parser("prepare-conversion")
    commands.add_parser("mark-converted")
    commands.add_parser("begin-authoring")
    commands.add_parser("begin-content-review")
    commands.add_parser("approve-content")
    commands.add_parser("begin-finalization")
    commands.add_parser("approve-final")
    commands.add_parser("complete")
    discover = commands.add_parser("discover-sources")
    discover.add_argument("--json", action="store_true", dest="as_json")
    blocked = commands.add_parser("block")
    blocked.add_argument("--owner", choices=("work", "codex"), required=True)
    blocked.add_argument("--reason", required=True)
    commands.add_parser("resume")
    current_url = commands.add_parser("set-current-url")
    current_url.add_argument("--url", required=True)
    commands.add_parser("clear-current-url")
    return result


def run(args: argparse.Namespace) -> int:
    root = repository_root(args.root)
    recover_repository_transactions(root)
    state = load_state(root)
    command = args.command
    if command == "status":
        command_status(root, state, as_json=args.as_json)
    elif command == "validate":
        print("deck.yaml: PASS")
    elif command == "migrate":
        report_path = _repo_path(root, str(args.report), field="migration.report") if args.report else None
        command_migrate(root, state, dry_run=args.dry_run, report_path=report_path)
    elif command == "discover-sources":
        selected, candidates = discover_source_candidates(root, state)
        payload = {"primarySource": selected.relative_to(root).as_posix() if selected else None, "candidates": [path.relative_to(root).as_posix() for path in candidates]}
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.as_json else "\n".join(payload["candidates"]))
    elif command == "initialize":
        command_initialize(root, state)
    elif command == "configure-chapters":
        command_configure_chapters(root, state, args.chapters)
    elif command == "configure-sections":
        command_configure_sections(root, state, args.sections)
    elif command == "submit-plan":
        command_submit_plan(root, state)
    elif command == "approve-plan":
        command_approve_plan(root, state)
    elif command == "begin-chapter":
        command_begin_chapter(root, state, args.chapter)
    elif command == "complete-chapter":
        command_complete_chapter(root, state, args.chapter)
    elif command == "approve-chapter":
        command_approve_chapter(root, state, args.chapter)
    elif command == "begin-section":
        command_begin_section(root, state, args.section)
    elif command == "complete-section":
        command_complete_section(root, state, args.section)
    elif command == "approve-section":
        command_approve_section(root, state, args.section)
    elif command == "unlock-section":
        command_unlock_section(root, state, args.section)
    elif command == "prepare-conversion":
        command_prepare_conversion(root, state)
    elif command == "mark-converted":
        command_mark_converted(root, state)
    elif command == "begin-authoring":
        command_begin_authoring(root, state)
    elif command == "begin-content-review":
        command_begin_content_review(root, state)
    elif command == "approve-content":
        command_approve_content(root, state)
    elif command == "begin-finalization":
        command_begin_finalization(root, state)
    elif command == "approve-final":
        command_approve_final(root, state)
    elif command == "complete":
        command_complete(root, state)
    elif command == "block":
        command_block(root, state, args.owner, args.reason)
    elif command == "resume":
        command_resume(root, state)
    elif command == "set-current-url":
        if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}/", args.url):
            raise WorkflowError("preview.currentUrl must be a 127.0.0.1 HTTP URL")
        port = int(args.url.rsplit(":", 1)[1].rstrip("/"))
        if port < 1 or port > 65535:
            raise WorkflowError("preview.currentUrl contains an invalid port")
        state["preview"]["currentUrl"] = args.url
        atomic_write_state(root, state)
    elif command == "clear-current-url":
        state["preview"]["currentUrl"] = None
        atomic_write_state(root, state)
    else:  # pragma: no cover
        raise WorkflowError(f"Unknown command: {command}")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run(parser().parse_args(argv))
    except (WorkflowError, BentoConverterError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
