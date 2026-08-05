"""Merge converted Bento segments into authoring documents without touching unrelated slides."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Iterable

from .authoring_storage import validate_authoring_document
from .errors import BentoConverterError
from .registry_document import REGISTRY_COLLECTIONS, normalize_registry, registry_revision
from .work_editor_storage import document_revision


ELEMENT_REFERENCE_FIELDS = {
    "elementId", "fromId", "toId", "sourceElementId", "targetElementId",
    "fromElementId", "toElementId", "anchorElementId",
}
SLIDE_REFERENCE_FIELDS = {
    "slideId", "targetSlideId", "sourceSlideId", "fromSlideId", "toSlideId",
}
RELATIONSHIP_FIELDS = {
    *ELEMENT_REFERENCE_FIELDS, *SLIDE_REFERENCE_FIELDS,
    "link", "href", "morphId", "stateOf", "transition", "connector",
}


def canonical_projection_hash(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def slide_hashes(document: dict[str, Any]) -> dict[str, str]:
    return {
        str(slide["id"]): canonical_projection_hash(slide)
        for slide in document.get("slides", []) if isinstance(slide, dict) and isinstance(slide.get("id"), str)
    }


def _walk_fields(value: Any, *, path: str = "") -> Iterable[tuple[str, str, Any]]:
    if isinstance(value, dict):
        for key, item in value.items():
            current = f"{path}.{key}" if path else key
            yield current, key, item
            yield from _walk_fields(item, path=current)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_fields(item, path=f"{path}[{index}]")


def relationship_projection(slide: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for path, key, value in _walk_fields(slide):
        if key in RELATIONSHIP_FIELDS:
            result.append({"path": path, "field": key, "value": value})
    return result


def _merge_registry(current: dict[str, Any], segment: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    merged = copy.deepcopy(normalize_registry(current, unit_id=str(current.get("unitId") or "deck")))
    incoming = normalize_registry(segment, unit_id=str(segment.get("unitId") or "segment"))
    changes: dict[str, Any] = {}
    for collection in ("sources", *REGISTRY_COLLECTIONS):
        destination = merged.setdefault(collection, {})
        added: list[str] = []
        shared: list[str] = []
        for identifier, definition in incoming.get(collection, {}).items():
            if identifier in destination:
                if destination[identifier] != definition:
                    raise BentoConverterError(
                        f"Segment registry conflicts with existing {collection}.{identifier}"
                    )
                shared.append(identifier)
            else:
                destination[identifier] = copy.deepcopy(definition)
                added.append(identifier)
        changes[collection] = {"added": sorted(added), "shared": sorted(shared)}
    current_document = merged.get("document", {})
    incoming_document = incoming.get("document", {})
    if current_document and incoming_document and current_document != incoming_document:
        raise BentoConverterError("Segment registry document metadata conflicts with the authoring registry")
    if not current_document and incoming_document:
        merged["document"] = copy.deepcopy(incoming_document)
    for field in ("slideIds", "elementIds", "requiredText"):
        values = list(merged.setdefault("protected", {}).get(field, []))
        for value in incoming.setdefault("protected", {}).get(field, []):
            if value not in values:
                values.append(value)
        merged["protected"][field] = values
    return merged, changes


def _all_element_ids(document: dict[str, Any]) -> set[str]:
    return {
        str(element["id"])
        for slide in document.get("slides", []) if isinstance(slide, dict)
        for element in slide.get("elements", []) if isinstance(element, dict) and isinstance(element.get("id"), str)
    }


def _validate_references(document: dict[str, Any]) -> list[dict[str, Any]]:
    slide_ids = {
        str(slide["id"]) for slide in document.get("slides", [])
        if isinstance(slide, dict) and isinstance(slide.get("id"), str)
    }
    element_ids = _all_element_ids(document)
    references: list[dict[str, Any]] = []
    errors: list[str] = []
    for slide in document.get("slides", []):
        if not isinstance(slide, dict):
            continue
        owner = str(slide.get("id"))
        for path, key, value in _walk_fields(slide):
            if key in SLIDE_REFERENCE_FIELDS and key != "slideId" and isinstance(value, str):
                references.append({"slideId": owner, "path": path, "field": key, "value": value})
                if value not in slide_ids:
                    errors.append(f"{owner}:{path} references missing slide {value!r}")
            if key in ELEMENT_REFERENCE_FIELDS and key != "elementId" and isinstance(value, str):
                references.append({"slideId": owner, "path": path, "field": key, "value": value})
                if value not in element_ids:
                    errors.append(f"{owner}:{path} references missing element {value!r}")
    if errors:
        raise BentoConverterError("Segment would create dangling references:\n- " + "\n- ".join(errors))
    return references


def _external_removed_element_references(
    current: dict[str, Any], *, target_slide_id: str, replacement: dict[str, Any],
) -> list[dict[str, Any]]:
    current_slide = next(slide for slide in current["slides"] if slide.get("id") == target_slide_id)
    old_ids = {str(item["id"]) for item in current_slide.get("elements", []) if isinstance(item, dict) and "id" in item}
    new_ids = {str(item["id"]) for item in replacement.get("elements", []) if isinstance(item, dict) and "id" in item}
    removed = old_ids - new_ids
    references: list[dict[str, Any]] = []
    for slide in current.get("slides", []):
        if not isinstance(slide, dict) or slide.get("id") == target_slide_id:
            continue
        for path, key, value in _walk_fields(slide):
            if key in ELEMENT_REFERENCE_FIELDS and isinstance(value, str) and value in removed:
                references.append({"slideId": slide.get("id"), "path": path, "field": key, "value": value})
    return references


def merge_segment(
    current_document: dict[str, Any], current_registry: dict[str, Any],
    segment_document: dict[str, Any], segment_registry: dict[str, Any],
    *, operation: str, slide_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Return a fully validated authoring document/registry merge and evidence report."""

    if operation not in {"import", "replace"}:
        raise BentoConverterError(f"Unsupported segment operation: {operation}")
    current = copy.deepcopy(current_document)
    segment_slides = [copy.deepcopy(slide) for slide in segment_document.get("slides", []) if isinstance(slide, dict)]
    if not segment_slides:
        raise BentoConverterError("Converted segment contains no slides")
    existing_ids = [str(slide.get("id")) for slide in current.get("slides", [])]
    incoming_ids = [str(slide.get("id")) for slide in segment_slides]
    if len(set(incoming_ids)) != len(incoming_ids):
        raise BentoConverterError("Converted segment contains duplicate slide IDs")
    before_hashes = slide_hashes(current)
    before_relationships: dict[str, Any] = {}
    replaced_ids: set[str] = set()
    if operation == "import":
        collisions = sorted(set(existing_ids) & set(incoming_ids))
        if collisions:
            raise BentoConverterError(f"Segment slide IDs already exist: {collisions}")
        current.setdefault("slides", []).extend(segment_slides)
    else:
        if not slide_id:
            raise BentoConverterError("Segment replace requires an explicit slide ID")
        if slide_id not in existing_ids:
            raise BentoConverterError(f"Replacement target slide does not exist: {slide_id}")
        if len(segment_slides) != 1 or incoming_ids != [slide_id]:
            raise BentoConverterError("Replacement segment must contain exactly the explicitly targeted slide ID")
        external = _external_removed_element_references(
            current, target_slide_id=slide_id, replacement=segment_slides[0],
        )
        if external:
            raise BentoConverterError(
                "Replacement removes elements referenced by other slides: "
                + json.dumps(external, ensure_ascii=False, sort_keys=True)
            )
        index = existing_ids.index(slide_id)
        before_relationships[slide_id] = relationship_projection(current["slides"][index])
        current["slides"][index] = segment_slides[0]
        replaced_ids.add(slide_id)
    merged_registry, registry_changes = _merge_registry(current_registry, segment_registry)
    references = _validate_references(current)
    validate_authoring_document(
        current, current=current_document, registry=merged_registry,
        explicit_replace_slide_ids=replaced_ids,
    )
    after_hashes = slide_hashes(current)
    unaffected = set(before_hashes) - replaced_ids
    changed_unexpectedly = sorted(
        identifier for identifier in unaffected
        if before_hashes.get(identifier) != after_hashes.get(identifier)
    )
    if changed_unexpectedly:
        raise BentoConverterError(f"Segment changed non-target slides: {changed_unexpectedly}")
    report = {
        "format": "bento/segment-operation-report/v1",
        "operation": f"segment-{operation}",
        "baseDocumentRevision": document_revision(current_document),
        "baseRegistryRevision": registry_revision(current_registry),
        "resultDocumentRevision": document_revision(current),
        "resultRegistryRevision": registry_revision(merged_registry),
        "slideIds": incoming_ids,
        "targetSlideId": slide_id,
        "unaffectedSlideHashes": {identifier: after_hashes[identifier] for identifier in sorted(unaffected)},
        "registry": registry_changes,
        "relationships": {
            "referencesAfter": references,
            "targetBefore": before_relationships,
            "targetAfter": {
                identifier: relationship_projection(next(slide for slide in current["slides"] if slide["id"] == identifier))
                for identifier in replaced_ids
            },
        },
        "validation": "pass",
    }
    return current, merged_registry, report
