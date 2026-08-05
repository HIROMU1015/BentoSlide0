"""Validate and advance the repository-centered BentoSlide workflow state."""

from __future__ import annotations

import argparse
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

from bento_converter.bento_validator import validate_bento_doc
from bento_converter.html_document import extract_bento_doc, load_html, runtime_fingerprint, serialize_bento_doc
from bento_converter.html_source import REGISTRY_FORMAT
from bento_converter.work_editor_storage import (
    WorkEditorStorage,
    document_revision,
    protected_content_fingerprint,
    validate_editor_document,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_RELATIVE = Path("workflow/deck.schema.json")
STATE_RELATIVE = Path("deck.yaml")
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
    "bento_finalization": "work",
    "complete": "codex",
}
STAGE_SOURCE = {
    "initialized": "sources",
    "planning": "planning",
    "awaiting_plan_approval": "planning",
    "html_authoring": "chapters",
    "html_review": "chapters",
    "ready_for_conversion": "chapters",
    "converting": "chapters",
    "bento_validation": "generated",
    "bento_finalization": "final",
    "complete": "final",
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


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"Cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise WorkflowError(f"{label} root must be an object: {path}")
    return value


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
    schema_path = root / SCHEMA_RELATIVE
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
    if stage != "blocked":
        if workflow["owner"] != STAGE_OWNER[stage]:
            raise WorkflowError(f"workflow.owner must be {STAGE_OWNER[stage]!r} for stage {stage!r}")
        if workflow["sourceOfTruth"] != STAGE_SOURCE[stage]:
            raise WorkflowError(f"workflow.sourceOfTruth must be {STAGE_SOURCE[stage]!r} for stage {stage!r}")
    current = workflow["currentChapter"]
    if current is not None and current not in state["chapters"]:
        raise WorkflowError(f"workflow.currentChapter is not registered: {current}")
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
        if blocked_from["sourceOfTruth"] != STAGE_SOURCE[previous_stage]:
            raise WorkflowError("workflow.blockedFrom.sourceOfTruth does not match its stage")
        previous_current = blocked_from["currentChapter"]
        if previous_current is not None and previous_current not in state["chapters"]:
            raise WorkflowError(f"workflow.blockedFrom.currentChapter is not registered: {previous_current}")
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
    resolved_outputs = {
        field: _repo_path(root, value, field=f"outputs.{field}")
        for field, value in state["outputs"].items()
    }
    if len(set(resolved_outputs.values())) != len(resolved_outputs):
        raise WorkflowError("Generated/final HTML and JSON output paths must be distinct")
    if resolved_outputs["generatedJson"] != _sidecar_path(resolved_outputs["generatedHtml"]):
        raise WorkflowError("outputs.generatedJson must be the sidecar path derived from outputs.generatedHtml")
    if resolved_outputs["finalJson"] != _sidecar_path(resolved_outputs["finalHtml"]):
        raise WorkflowError("outputs.finalJson must be the sidecar path derived from outputs.finalHtml")
    baseline = state["validation"].get("finalBaseline")
    if baseline is not None:
        baseline_path = _repo_path(root, baseline["path"], field="validation.finalBaseline.path")
        if baseline_path != _final_baseline_path(root, state):
            raise WorkflowError("validation.finalBaseline.path does not match outputs.finalHtml")
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
    workflow["sourceOfTruth"] = STAGE_SOURCE[stage]
    workflow["currentChapter"] = current
    workflow["blockingReason"] = None
    workflow["blockedFrom"] = None


def _require_stage(state: dict[str, Any], *allowed: str) -> None:
    actual = state["workflow"]["stage"]
    if actual not in allowed:
        raise WorkflowError(f"Stage {actual!r} does not allow this operation; expected one of {allowed}")


def discover_source_candidates(root: Path, state: dict[str, Any]) -> tuple[Path | None, list[Path]]:
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
    path = _repo_path(root, metadata["path"], field="validation.finalBaseline.path")
    if not path.is_file():
        raise WorkflowError(f"Final content baseline does not exist: {metadata['path']}")
    document = _load_sidecar(path)
    if document_revision(document) != metadata["documentRevision"]:
        raise WorkflowError("Final content baseline document revision does not match deck.yaml")
    fingerprint = protected_content_fingerprint(document)
    if fingerprint != metadata["protectedContentFingerprint"]:
        raise WorkflowError("Final content baseline fingerprint does not match deck.yaml")
    return document, fingerprint


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
    required = [
        output_root / "conversion-report.json",
        output_root / "diagnostics/computed-layout.json",
        output_root / "diagnostics/merged-registry.json",
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
    registry = _read_json(required[2], label="merged registry")
    baseline_document, baseline_fingerprint = _baseline_document(
        root, state, generated_document, allow_missing=allow_missing_baseline,
    )
    validate_editor_document(final_document, current=baseline_document, registry=registry, allow_content_edit=False)
    if protected_content_fingerprint(final_document) != baseline_fingerprint:
        raise WorkflowError(
            "Final Bento content/structure differs from its finalization baseline; only presentation edits are allowed"
        )
    if runtime_fingerprint(final_html) != result["generatedRuntime"]:
        raise WorkflowError("Final Bento runtime differs from generated runtime")
    result["finalDocument"] = final_document
    return result


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


def command_submit_plan(root: Path, state: dict[str, Any]) -> None:
    _require_stage(state, "planning")
    validate_planning(root)
    if not state["chapters"]:
        raise WorkflowError("Register the planned chapters before requesting approval")
    _transition(state, "awaiting_plan_approval", "awaiting_approval")
    atomic_write_state(root, state)
    append_work_log(root, "Submitted explanation policy, story outline, and slide plan for approval")


def command_approve_plan(root: Path, state: dict[str, Any]) -> None:
    _require_stage(state, "awaiting_plan_approval")
    validate_planning(root)
    if not state["chapters"]:
        raise WorkflowError("No chapters are configured")
    for key in ("explanationPolicy", "storyOutline", "slidePlan"):
        state["approvals"][key] = "approved"
    first = next(iter(state["chapters"]))
    _transition(state, "html_authoring", "in_progress", current=first)
    atomic_write_state(root, state)
    append_work_log(root, "Recorded plan approval and opened HTML authoring")


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
    validate_chapters(root, state, require_complete=True)
    if not state["handoff"]["readyForCodex"]:
        raise WorkflowError("Work-to-Codex handoff is not ready")
    _transition(state, "converting", "in_progress")
    atomic_write_state(root, state)
    append_work_log(root, "Validated conversion readiness and handed the deck to Codex")


def command_mark_converted(root: Path, state: dict[str, Any]) -> None:
    _require_stage(state, "converting")
    bundle = validate_output_bundle(root, state, require_final=False)
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


def command_begin_finalization(root: Path, state: dict[str, Any]) -> None:
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
            if not state["chapters"]:
                raise WorkflowError("Cannot resume plan approval without configured chapters")
        return
    if any(state["approvals"][key] != "approved" for key in ("explanationPolicy", "storyOutline", "slidePlan")):
        raise WorkflowError(f"Cannot resume {stage!r} while plan approvals are incomplete")
    if not state["chapters"]:
        raise WorkflowError(f"Cannot resume {stage!r} without configured chapters")
    if stage == "html_authoring":
        return
    if stage == "html_review":
        current = snapshot["currentChapter"]
        if not current:
            raise WorkflowError("Cannot resume HTML review without a current chapter")
        entry = state["chapters"][current]
        if entry["status"] != "review":
            raise WorkflowError(f"Chapter is not awaiting review: {current}")
        _load_chapter(root, current, entry)
        return
    if stage in {"ready_for_conversion", "converting"}:
        validate_chapters(root, state, require_complete=True)
        if not state["handoff"]["readyForCodex"]:
            raise WorkflowError("Cannot resume conversion because the Work-to-Codex handoff is not ready")
        return
    if stage == "bento_validation":
        validate_output_bundle(root, state, require_final=True)
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
    commands.add_parser("initialize")
    configure = commands.add_parser("configure-chapters")
    configure.add_argument("chapters", nargs="+")
    commands.add_parser("submit-plan")
    commands.add_parser("approve-plan")
    for name in ("begin-chapter", "complete-chapter", "approve-chapter"):
        child = commands.add_parser(name)
        child.add_argument("--chapter")
    commands.add_parser("prepare-conversion")
    commands.add_parser("mark-converted")
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
    state = load_state(root)
    command = args.command
    if command == "status":
        command_status(root, state, as_json=args.as_json)
    elif command == "validate":
        print("deck.yaml: PASS")
    elif command == "discover-sources":
        selected, candidates = discover_source_candidates(root, state)
        payload = {"primarySource": selected.relative_to(root).as_posix() if selected else None, "candidates": [path.relative_to(root).as_posix() for path in candidates]}
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.as_json else "\n".join(payload["candidates"]))
    elif command == "initialize":
        command_initialize(root, state)
    elif command == "configure-chapters":
        command_configure_chapters(root, state, args.chapters)
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
    elif command == "prepare-conversion":
        command_prepare_conversion(root, state)
    elif command == "mark-converted":
        command_mark_converted(root, state)
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
    except (WorkflowError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
