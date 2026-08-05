"""Canonical registry validation, normalization, and revision helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .errors import BentoConverterError


REGISTRY_V1 = "bento/html-registry/v1"
REGISTRY_V2 = "bento/html-registry/v2"
REGISTRY_COLLECTIONS = ("assets", "fonts", "equations", "figures", "tables", "charts")


def canonical_registry_json(registry: dict[str, Any]) -> str:
    return json.dumps(
        registry,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def registry_revision(registry: dict[str, Any]) -> str:
    payload = canonical_registry_json(registry).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def load_registry(path: str | Path) -> dict[str, Any]:
    registry_path = Path(path)
    try:
        value = json.loads(registry_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise BentoConverterError(f"Cannot read registry {registry_path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BentoConverterError(f"Registry root must be an object: {registry_path}")
    return value


def validate_registry(registry: dict[str, Any], *, allow_v1: bool = True) -> None:
    formats = {REGISTRY_V2, REGISTRY_V1} if allow_v1 else {REGISTRY_V2}
    if registry.get("format") not in formats:
        raise BentoConverterError(f"Unsupported registry format: {registry.get('format')!r}")
    if registry.get("format") == REGISTRY_V2:
        unit_id = registry.get("unitId")
        if not isinstance(unit_id, str) or not unit_id:
            raise BentoConverterError("Registry v2 unitId must be a non-empty string")
        sources = registry.get("sources", {})
        if not isinstance(sources, dict):
            raise BentoConverterError("Registry v2 sources must be an object")
        for source_id, source in sources.items():
            if not isinstance(source_id, str) or not source_id or not isinstance(source, dict):
                raise BentoConverterError("Registry v2 source definitions must be objects keyed by stable IDs")
            if not isinstance(source.get("path"), str) or not source["path"]:
                raise BentoConverterError(f"Registry source {source_id!r} requires a path")
    for collection in REGISTRY_COLLECTIONS:
        value = registry.get(collection, {})
        if not isinstance(value, dict):
            raise BentoConverterError(f"Registry {collection} must be an object")
    protected = registry.get("protected", {})
    if not isinstance(protected, dict):
        raise BentoConverterError("Registry protected must be an object")
    for field in ("slideIds", "elementIds", "requiredText"):
        values = protected.get(field, [])
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise BentoConverterError(f"Registry protected.{field} must be an array of strings")


def normalize_registry(
    registry: dict[str, Any], *, unit_id: str = "deck", source_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize v1/v2 registry data to the v2 lifecycle representation."""

    validate_registry(registry, allow_v1=True)
    if registry.get("format") == REGISTRY_V2:
        return json.loads(json.dumps(registry, ensure_ascii=False))
    sources: dict[str, Any] = {}
    if source_manifest:
        for item in source_manifest.get("items", []):
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                sources[item["id"]] = {
                    key: item[key] for key in ("path", "type", "role") if key in item
                }
    normalized: dict[str, Any] = {
        "format": REGISTRY_V2,
        "unitId": unit_id,
        "sources": sources,
        "document": registry.get("document", {}),
        "protected": registry.get("protected", {"slideIds": [], "elementIds": [], "requiredText": []}),
    }
    for collection in REGISTRY_COLLECTIONS:
        normalized[collection] = registry.get(collection, {})
    paper_source = registry.get("paperSource")
    if paper_source is not None:
        normalized["paperSource"] = paper_source
    validate_registry(normalized, allow_v1=False)
    return normalized
