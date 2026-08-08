"""Merge converted Bento segments into authoring documents without touching unrelated slides."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Iterable

from .authoring_storage import REFERENCE_FIELDS, validate_authoring_document, visible_document_text
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


def registry_dependency_closure(
    value: Any, registry: dict[str, Any], *, strict: bool = True,
) -> dict[str, set[str]]:
    """Return registry definitions transitively required by a document fragment."""

    normalized = normalize_registry(registry, unit_id=str(registry.get("unitId") or "deck"))
    references = {collection: set() for collection in REFERENCE_FIELDS.values()}
    references["sources"] = set()
    visited = {collection: set() for collection in references}

    def collect(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                collection = REFERENCE_FIELDS.get(key)
                if collection and isinstance(child, str) and child:
                    references[collection].add(child)
                if key == "sourceId" and isinstance(child, str) and child:
                    references["sources"].add(child)
                collect(child)
        elif isinstance(item, list):
            for child in item:
                collect(child)

    collect(value)
    while True:
        pending = {
            collection: sorted(references[collection] - visited[collection])
            for collection in references
        }
        if not any(pending.values()):
            break
        for collection, identifiers in pending.items():
            definitions = normalized.get(collection, {})
            for identifier in identifiers:
                visited[collection].add(identifier)
                definition = definitions.get(identifier)
                if definition is None:
                    if strict:
                        raise BentoConverterError(
                            f"Document fragment references missing registry definition: {collection}.{identifier}"
                        )
                    continue
                collect(definition)
    return visited


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


def _external_removed_references(
    current: dict[str, Any], *, target_slide_ids: set[str], replacements: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    old_elements = {
        str(element["id"])
        for slide in current.get("slides", [])
        if isinstance(slide, dict) and slide.get("id") in target_slide_ids
        for element in slide.get("elements", [])
        if isinstance(element, dict) and isinstance(element.get("id"), str)
    }
    new_slide_ids = {
        str(slide["id"]) for slide in replacements if isinstance(slide.get("id"), str)
    }
    new_elements = {
        str(element["id"])
        for slide in replacements
        for element in slide.get("elements", [])
        if isinstance(element, dict) and isinstance(element.get("id"), str)
    }
    removed_slides = target_slide_ids - new_slide_ids
    removed_elements = old_elements - new_elements
    references: list[dict[str, Any]] = []
    for slide in current.get("slides", []):
        if not isinstance(slide, dict) or slide.get("id") in target_slide_ids:
            continue
        for path, key, value in _walk_fields(slide):
            missing = None
            if key in SLIDE_REFERENCE_FIELDS and key != "slideId" and isinstance(value, str) and value in removed_slides:
                missing = "slide"
            elif key in ELEMENT_REFERENCE_FIELDS and key != "elementId" and isinstance(value, str) and value in removed_elements:
                missing = "element"
            elif key in {"link", "href"} and isinstance(value, str):
                reference = value[1:] if value.startswith("#") else value
                if reference in removed_slides:
                    missing = "slide"
                elif reference in removed_elements:
                    missing = "element"
            if missing:
                references.append({
                    "slideId": slide.get("id"), "path": path, "field": key,
                    "value": value, "missing": missing,
                })
    return references


def _registry_without_replaced_section(
    registry: dict[str, Any], *, current: dict[str, Any], target_slide_ids: set[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Remove target-local definitions/protection before merging a section replacement."""

    prepared = copy.deepcopy(normalize_registry(registry, unit_id=str(registry.get("unitId") or "deck")))
    target_slides = [
        slide for slide in current.get("slides", [])
        if isinstance(slide, dict) and slide.get("id") in target_slide_ids
    ]
    remaining_document = copy.deepcopy(current)
    remaining_document["slides"] = [
        slide for slide in remaining_document.get("slides", [])
        if not isinstance(slide, dict) or slide.get("id") not in target_slide_ids
    ]
    target_dependencies = registry_dependency_closure(target_slides, prepared)
    remaining_dependencies = registry_dependency_closure(remaining_document["slides"], prepared)
    removed_definitions: dict[str, list[str]] = {}
    for collection in ("sources", *REGISTRY_COLLECTIONS):
        local = sorted(
            target_dependencies.get(collection, set())
            - remaining_dependencies.get(collection, set())
        )
        destination = prepared.setdefault(collection, {})
        removed_definitions[collection] = [identifier for identifier in local if identifier in destination]
        for identifier in removed_definitions[collection]:
            del destination[identifier]
    target_element_ids = {
        str(element["id"])
        for slide in target_slides
        for element in slide.get("elements", [])
        if isinstance(element, dict) and isinstance(element.get("id"), str)
    }
    remaining_text = visible_document_text(remaining_document)
    protected = prepared.setdefault("protected", {})
    removed: dict[str, Any] = {
        "definitions": removed_definitions,
        "protected": {"slideIds": [], "elementIds": [], "requiredText": []},
    }
    for field, owned in (("slideIds", target_slide_ids), ("elementIds", target_element_ids)):
        values = list(protected.get(field, []))
        removed["protected"][field] = sorted(str(value) for value in values if value in owned)
        protected[field] = [value for value in values if value not in owned]
    required = list(protected.get("requiredText", []))
    removed["protected"]["requiredText"] = sorted(
        str(value) for value in required if value not in remaining_text
    )
    protected["requiredText"] = [value for value in required if value in remaining_text]
    return prepared, removed


def merge_segment(
    current_document: dict[str, Any], current_registry: dict[str, Any],
    segment_document: dict[str, Any], segment_registry: dict[str, Any],
    *, operation: str, slide_id: str | None = None, anchor_slide_id: str | None = None,
    target_slide_ids: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Return a fully validated authoring document/registry merge and evidence report."""

    aliases = {"import": "append", "replace": "replace-slide"}
    normalized_operation = aliases.get(operation, operation)
    if normalized_operation not in {
        "append", "insert-before", "insert-after", "replace-slide", "replace-range", "replace-section",
    }:
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
    if normalized_operation in {"append", "insert-before", "insert-after"}:
        collisions = sorted(set(existing_ids) & set(incoming_ids))
        if collisions:
            raise BentoConverterError(f"Segment slide IDs already exist: {collisions}")
        if normalized_operation == "append":
            index = len(existing_ids)
        else:
            if not anchor_slide_id or anchor_slide_id not in existing_ids:
                raise BentoConverterError("Ordered segment insertion requires an existing anchor slide ID")
            index = existing_ids.index(anchor_slide_id)
            if normalized_operation == "insert-after":
                index += 1
        current.setdefault("slides", [])[index:index] = segment_slides
    else:
        targets = list(target_slide_ids or ([] if slide_id is None else [slide_id]))
        if normalized_operation == "replace-slide":
            if len(targets) != 1 or len(segment_slides) != 1 or incoming_ids != targets:
                raise BentoConverterError("Replacement segment must preserve exactly the explicit slide ID")
        if not targets or any(target not in existing_ids for target in targets):
            raise BentoConverterError("Replacement targets must be existing slide IDs")
        indexes = [existing_ids.index(target) for target in targets]
        if indexes != list(range(indexes[0], indexes[0] + len(indexes))):
            raise BentoConverterError("Range and section replacement targets must be contiguous and ordered")
        if normalized_operation == "replace-range" and len(segment_slides) != len(targets):
            raise BentoConverterError("Range replacement must keep the same number of slides")
        if normalized_operation == "replace-range" and incoming_ids != targets:
            raise BentoConverterError("Range replacement must preserve target slide IDs and order")
        if set(incoming_ids) & (set(existing_ids) - set(targets)):
            raise BentoConverterError("Replacement slide IDs collide with non-target slides")
        if normalized_operation == "replace-slide":
            external = _external_removed_element_references(
                current, target_slide_id=targets[0], replacement=segment_slides[0],
            )
            if external:
                raise BentoConverterError(
                    "Replacement removes elements referenced by other slides: "
                    + json.dumps(external, ensure_ascii=False, sort_keys=True)
                )
        elif normalized_operation == "replace-section":
            external = _external_removed_references(
                current, target_slide_ids=set(targets), replacements=segment_slides,
            )
            if external:
                raise BentoConverterError(
                    "Section replacement removes slides or elements referenced by other slides: "
                    + json.dumps(external, ensure_ascii=False, sort_keys=True)
                )
        for target in targets:
            before_relationships[target] = relationship_projection(current["slides"][existing_ids.index(target)])
        current["slides"][indexes[0]:indexes[-1] + 1] = segment_slides
        replaced_ids.update(targets)
    registry_base = current_registry
    removed_section_registry: dict[str, Any] = {
        "definitions": {collection: [] for collection in ("sources", *REGISTRY_COLLECTIONS)},
        "protected": {"slideIds": [], "elementIds": [], "requiredText": []},
    }
    if normalized_operation == "replace-section":
        registry_base, removed_section_registry = _registry_without_replaced_section(
            current_registry, current=current_document, target_slide_ids=replaced_ids,
        )
    merged_registry, registry_changes = _merge_registry(registry_base, segment_registry)
    # Preserve the existing report field for callers while reporting the new
    # target-local definition pruning separately.
    registry_changes["protectedRemoved"] = removed_section_registry["protected"]
    registry_changes["definitionsRemoved"] = removed_section_registry["definitions"]
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
        "operation": f"segment-{normalized_operation}",
        "baseDocumentRevision": document_revision(current_document),
        "baseRegistryRevision": registry_revision(current_registry),
        "resultDocumentRevision": document_revision(current),
        "resultRegistryRevision": registry_revision(merged_registry),
        "slideIds": incoming_ids,
        "targetSlideId": slide_id,
        "anchorSlideId": anchor_slide_id,
        "targetSlideIds": sorted(replaced_ids),
        "unaffectedSlideHashes": {identifier: after_hashes[identifier] for identifier in sorted(unaffected)},
        "registry": registry_changes,
        "relationships": {
            "referencesAfter": references,
            "targetBefore": before_relationships,
            "targetAfter": {
                identifier: relationship_projection(next(slide for slide in current["slides"] if slide["id"] == identifier))
                for identifier in incoming_ids
            },
        },
        "validation": "pass",
    }
    return current, merged_registry, report
