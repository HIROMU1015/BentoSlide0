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
VISUAL_ORIGIN_KINDS = {"source-original", "source-derived", "generated"}
GENERATED_FORBIDDEN_ROLES = {
    "data", "experimental-result", "measurement", "benchmark",
    "quantitative-plot", "equation",
}


def visual_origin_source_ids(definition: dict[str, Any]) -> set[str]:
    """Return source IDs named by a visual origin without treating generated art as evidence."""

    origin = definition.get("origin")
    if not isinstance(origin, dict):
        return set()
    if origin.get("kind") == "source-original":
        source_id = origin.get("sourceId")
        return {source_id} if isinstance(source_id, str) and source_id else set()
    if origin.get("kind") == "source-derived":
        return {
            item["sourceId"] for item in origin.get("sources", [])
            if isinstance(item, dict) and isinstance(item.get("sourceId"), str) and item["sourceId"]
        }
    return set()


def _validate_visual_origin(
    registry: dict[str, Any], collection: str, definition_id: str, definition: dict[str, Any],
) -> None:
    origin = definition.get("origin")
    if origin is None:
        return
    label = f"Registry {collection}.{definition_id}.origin"
    if collection not in {"assets", "figures"}:
        raise BentoConverterError(f"{label} is supported only for assets and figures")
    if not isinstance(origin, dict):
        raise BentoConverterError(f"{label} must be an object")
    kind = origin.get("kind")
    if kind not in VISUAL_ORIGIN_KINDS:
        raise BentoConverterError(f"{label}.kind must be one of {sorted(VISUAL_ORIGIN_KINDS)}")
    sources = registry.get("sources", {})
    if kind == "source-original":
        source_id = origin.get("sourceId")
        locator = origin.get("locator")
        if not isinstance(source_id, str) or not source_id:
            raise BentoConverterError(f"{label} source-original requires sourceId")
        if source_id not in sources:
            raise BentoConverterError(f"{label} references unknown sourceId {source_id!r}")
        if not isinstance(locator, str) or not locator.strip():
            raise BentoConverterError(f"{label} source-original requires a non-empty locator")
        if "sources" in origin:
            raise BentoConverterError(f"{label} source-original must not define sources")
    elif kind == "source-derived":
        derived_sources = origin.get("sources")
        if not isinstance(derived_sources, list) or not derived_sources:
            raise BentoConverterError(f"{label} source-derived requires a non-empty sources array")
        for index, item in enumerate(derived_sources):
            if not isinstance(item, dict):
                raise BentoConverterError(f"{label}.sources[{index}] must be an object")
            source_id = item.get("sourceId")
            locator = item.get("locator")
            if not isinstance(source_id, str) or not source_id:
                raise BentoConverterError(f"{label}.sources[{index}] requires sourceId")
            if source_id not in sources:
                raise BentoConverterError(f"{label} references unknown sourceId {source_id!r}")
            if not isinstance(locator, str) or not locator.strip():
                raise BentoConverterError(f"{label}.sources[{index}] requires a non-empty locator")
        if "sourceId" in origin or "locator" in origin:
            raise BentoConverterError(f"{label} source-derived must use sources, not sourceId/locator")
    else:
        if any(field in origin for field in ("sourceId", "sources", "locator")):
            raise BentoConverterError(f"{label} generated must not claim source provenance")
        if definition.get("provenance") is not None:
            raise BentoConverterError(f"Registry {collection}.{definition_id} generated visual must not claim provenance")
        if definition.get("role") in GENERATED_FORBIDDEN_ROLES:
            raise BentoConverterError(
                f"Registry {collection}.{definition_id} generated visual cannot have role {definition['role']!r}"
            )


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
            source_path = Path(source["path"])
            if source_path.is_absolute() or ".." in source_path.parts:
                raise BentoConverterError(f"Registry source {source_id!r} path must be repository-relative")
    for collection in REGISTRY_COLLECTIONS:
        value = registry.get(collection, {})
        if not isinstance(value, dict):
            raise BentoConverterError(f"Registry {collection} must be an object")
        for definition_id, definition in value.items():
            if not isinstance(definition_id, str) or not definition_id or not isinstance(definition, dict):
                raise BentoConverterError(f"Registry {collection} definitions must be objects keyed by stable IDs")
            if registry.get("format") == REGISTRY_V2:
                _validate_visual_origin(registry, collection, definition_id, definition)
            provenance = definition.get("provenance")
            if provenance is None:
                continue
            if not isinstance(provenance, dict):
                raise BentoConverterError(f"Registry {collection}.{definition_id}.provenance must be an object")
            if registry.get("format") != REGISTRY_V2:
                continue
            source_id = provenance.get("sourceId")
            if not isinstance(source_id, str) or not source_id:
                raise BentoConverterError(f"Registry {collection}.{definition_id}.provenance requires sourceId")
            if source_id not in registry.get("sources", {}):
                raise BentoConverterError(f"Registry {collection}.{definition_id} references unknown sourceId {source_id!r}")
            if "locator" in provenance and not isinstance(provenance["locator"], str):
                raise BentoConverterError(f"Registry {collection}.{definition_id}.provenance.locator must be a string")
            origin = definition.get("origin")
            if isinstance(origin, dict) and origin.get("kind") == "source-original":
                if provenance.get("sourceId") != origin.get("sourceId") or provenance.get("locator") != origin.get("locator"):
                    raise BentoConverterError(
                        f"Registry {collection}.{definition_id} provenance must match source-original origin"
                    )
    figures = registry.get("figures", {})
    assets = registry.get("assets", {})
    for figure_id, figure in figures.items():
        asset_id = figure.get("assetId") if isinstance(figure, dict) else None
        if asset_id is None:
            continue
        figure_origin = figure.get("origin")
        if figure_origin is None:
            continue
        if not isinstance(asset_id, str) or asset_id not in assets:
            raise BentoConverterError(f"Registry figures.{figure_id}.assetId references an unknown asset")
        asset_origin = assets[asset_id].get("origin") if isinstance(assets[asset_id], dict) else None
        if not isinstance(asset_origin, dict) or figure_origin != asset_origin:
            raise BentoConverterError(f"Registry figures.{figure_id}.origin must match assets.{asset_id}.origin")
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
