"""Discover and validate HTML-first Bento source chapters and registries."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import BentoConverterError, JsonLoadError, ValidationError, issue
from .registry_document import REGISTRY_V1, REGISTRY_V2, validate_registry, validate_registry_asset_content

REGISTRY_FORMAT = REGISTRY_V1


@dataclass(frozen=True)
class SourceUnit:
    chapter_id: str
    html_path: Path
    registry_path: Path
    registry: dict[str, Any]

    @property
    def unit_id(self) -> str:
        return self.chapter_id


# Public compatibility alias. The converter still uses ``chapter_id`` internally
# because the computed-layout report format predates single-file source units.
SourceChapter = SourceUnit


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JsonLoadError(f"Cannot read registry JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise JsonLoadError(f"Registry root must be an object: {path}")
    return value


def _registry_name(html_path: Path) -> str:
    name = html_path.name
    if name.endswith(".preview.html"):
        return name[: -len(".preview.html")] + ".registry.json"
    return html_path.stem + ".registry.json"


def _validate_source_registry(registry: dict[str, Any], *, expected_unit: str | None = None) -> tuple[str | None, list[str]]:
    errors: list[str] = []
    try:
        validate_registry(registry, allow_v1=True)
    except BentoConverterError as exc:
        return None, [f"{exc}; use {REGISTRY_V1!r} or {REGISTRY_V2!r}"]
    unit_id = registry.get("unitId") if registry.get("format") == REGISTRY_V2 else registry.get("chapterId")
    label = "unitId" if registry.get("format") == REGISTRY_V2 else "chapterId"
    if not isinstance(unit_id, str) or not unit_id.strip():
        errors.append(issue(field=label, actual=unit_id, fix="Provide a non-empty source unit id."))
    elif expected_unit is not None and unit_id != expected_unit:
        errors.append(issue(field=label, actual=unit_id, fix=f"Use {expected_unit!r}."))
    for equation_id, equation in registry.get("equations", {}).items():
        if not isinstance(equation, dict) or not isinstance(equation.get("latex"), str) or not equation["latex"].strip():
            errors.append(issue(element_id=equation_id, field="equations.*.latex", actual=equation, fix="Provide the original non-empty LaTeX source."))
    return unit_id if isinstance(unit_id, str) else None, errors


def discover_source_unit(html: str | Path, registry: str | Path) -> SourceUnit:
    """Load one explicit HTML/registry pair as a normalized source unit."""

    html_path = Path(html)
    registry_path = Path(registry)
    if not html_path.is_file():
        raise JsonLoadError(f"HTML source does not exist: {html_path}")
    if not registry_path.is_file():
        raise JsonLoadError(f"Registry source does not exist: {registry_path}")
    document = _load_json(registry_path)
    unit_id, errors = _validate_source_registry(document)
    if errors:
        raise ValidationError(errors)
    validate_registry_asset_content(document, asset_base=registry_path.parent)
    assert unit_id is not None
    return SourceUnit(unit_id, html_path.resolve(), registry_path.resolve(), document)


def discover_chapters(html_dir: str | Path, registry_dir: str | Path) -> list[SourceUnit]:
    html_root = Path(html_dir)
    registry_root = Path(registry_dir)
    if not html_root.is_dir():
        raise JsonLoadError(f"HTML directory does not exist: {html_root}")
    if not registry_root.is_dir():
        raise JsonLoadError(f"Registry directory does not exist: {registry_root}")
    html_paths = sorted(
        path for path in html_root.glob("*.html")
        if not path.name.endswith(".bento.html")
    )
    if not html_paths:
        raise JsonLoadError(f"No chapter HTML files found in {html_root}")

    chapters: list[SourceUnit] = []
    errors: list[str] = []
    chapter_ids: set[str] = set()
    for html_path in html_paths:
        registry_path = registry_root / _registry_name(html_path)
        if not registry_path.is_file():
            errors.append(
                issue(field="registry", actual=str(registry_path), fix=f"Create {_registry_name(html_path)} for {html_path.name}.")
            )
            continue
        registry = _load_json(registry_path)
        chapter_id, registry_errors = _validate_source_registry(registry)
        errors.extend(registry_errors)
        if not registry_errors:
            try:
                validate_registry_asset_content(registry, asset_base=registry_path.parent)
            except BentoConverterError as exc:
                errors.append(str(exc))
        if registry.get("format") != REGISTRY_FORMAT:
            errors.append(issue(field="format", actual=registry.get("format"), fix=f"Use {REGISTRY_FORMAT!r} for modular chapter discovery."))
        if not chapter_id:
            continue
        if chapter_id in chapter_ids:
            errors.append(issue(field="chapterId", actual=chapter_id, fix="Use a unique chapter id."))
            continue
        chapter_ids.add(chapter_id)
        chapters.append(SourceUnit(chapter_id, html_path.resolve(), registry_path.resolve(), registry))
    if errors:
        raise ValidationError(errors)
    return chapters


def merge_registries(chapters: list[SourceUnit]) -> dict[str, Any]:
    """Merge chapter registries while rejecting ambiguous global identifiers."""

    use_v2 = any(chapter.registry.get("format") == REGISTRY_V2 for chapter in chapters)
    merged: dict[str, Any] = {
        "format": REGISTRY_V2 if use_v2 else REGISTRY_FORMAT,
        "document": {},
        "assets": {},
        "fonts": {},
        "equations": {},
        "figures": {},
        "tables": {},
        "charts": {},
        "protected": {"slideIds": [], "elementIds": [], "requiredText": []},
    }
    if use_v2:
        merged["unitId"] = chapters[0].unit_id if len(chapters) == 1 else "deck"
        merged["sources"] = {}
    errors: list[str] = []
    for chapter in chapters:
        if use_v2:
            for source_id, source in chapter.registry.get("sources", {}).items():
                previous = merged["sources"].get(source_id)
                if previous is not None and previous != source:
                    errors.append(issue(element_id=source_id, field="sources", actual=source, fix="Use globally unique source ids or identical definitions."))
                else:
                    merged["sources"][source_id] = source
        document = chapter.registry.get("document", {})
        if document and not isinstance(document, dict):
            errors.append(issue(field=f"{chapter.chapter_id}.document", actual=document, fix="Use an object."))
        elif document:
            for key, value in document.items():
                previous = merged["document"].get(key)
                if previous is not None and previous != value:
                    errors.append(issue(field=f"document.{key}", actual=value, fix=f"Use the same value in every chapter; first value is {previous!r}."))
                else:
                    merged["document"][key] = value
        for collection in ("assets", "fonts", "equations", "figures", "tables", "charts"):
            for key, value in chapter.registry.get(collection, {}).items():
                if key in merged[collection] and merged[collection][key] != value:
                    errors.append(issue(element_id=key, field=collection, actual=value, fix="Use globally unique registry ids or identical definitions."))
                else:
                    merged[collection][key] = value
        protected = chapter.registry.get("protected", {})
        if protected and not isinstance(protected, dict):
            errors.append(issue(field="protected", actual=protected, fix="Use an object of id/text arrays."))
            continue
        for key in ("slideIds", "elementIds", "requiredText"):
            values = protected.get(key, [])
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                errors.append(issue(field=f"protected.{key}", actual=values, fix="Use an array of strings."))
            else:
                merged["protected"][key].extend(value for value in values if value not in merged["protected"][key])
    if errors:
        raise ValidationError(errors)
    return merged
