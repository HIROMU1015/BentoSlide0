"""Discover and validate HTML-first Bento source chapters and registries."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import JsonLoadError, ValidationError, issue

REGISTRY_FORMAT = "bento/html-registry/v1"


@dataclass(frozen=True)
class SourceChapter:
    chapter_id: str
    html_path: Path
    registry_path: Path
    registry: dict[str, Any]


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


def discover_chapters(html_dir: str | Path, registry_dir: str | Path) -> list[SourceChapter]:
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

    chapters: list[SourceChapter] = []
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
        chapter_id = registry.get("chapterId")
        if registry.get("format") != REGISTRY_FORMAT:
            errors.append(issue(field="format", actual=registry.get("format"), fix=f"Use {REGISTRY_FORMAT!r}."))
        if not isinstance(chapter_id, str) or not chapter_id.strip():
            errors.append(issue(field="chapterId", actual=chapter_id, fix="Provide a non-empty chapter id."))
            continue
        if chapter_id in chapter_ids:
            errors.append(issue(field="chapterId", actual=chapter_id, fix="Use a unique chapter id."))
            continue
        chapter_ids.add(chapter_id)
        for collection in ("assets", "fonts", "equations", "figures", "tables", "charts"):
            if collection in registry and not isinstance(registry[collection], dict):
                errors.append(issue(field=collection, actual=registry[collection], fix="Use an object keyed by stable registry id."))
        for equation_id, equation in registry.get("equations", {}).items():
            if not isinstance(equation, dict) or not isinstance(equation.get("latex"), str) or not equation["latex"].strip():
                errors.append(issue(element_id=equation_id, field="equations.*.latex", actual=equation, fix="Provide the original non-empty LaTeX source."))
        chapters.append(SourceChapter(chapter_id, html_path.resolve(), registry_path.resolve(), registry))
    if errors:
        raise ValidationError(errors)
    return chapters


def merge_registries(chapters: list[SourceChapter]) -> dict[str, Any]:
    """Merge chapter registries while rejecting ambiguous global identifiers."""

    merged: dict[str, Any] = {
        "format": REGISTRY_FORMAT,
        "document": {},
        "assets": {},
        "fonts": {},
        "equations": {},
        "figures": {},
        "tables": {},
        "charts": {},
        "protected": {"slideIds": [], "elementIds": [], "requiredText": []},
    }
    errors: list[str] = []
    for chapter in chapters:
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
