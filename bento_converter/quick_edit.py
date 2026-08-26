"""Compile semantic Bento edits into revision-checked primitive browser edits."""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .bento_validator import validate_bento_doc
from .browser_edit import (
    PLAN_FORMAT,
    BrowserEditPostSaveError,
    run_browser_edit,
    validate_browser_edit_plan,
)
from .errors import BentoConverterError
from .work_editor_client import WorkEditorClient


QUICK_PLAN_FORMAT = "bento/quick-edit-plan/v1"
QUICK_REPORT_FORMAT = "bento/quick-edit-report/v1"

GROUPS = {
    "all", "all-elements", "body", "header", "footer", "title",
    "text", "shapes", "body-text", "body-shapes",
}
QUICK_OPERATIONS = {
    "shift", "text-style", "shape-style", "resize-group", "align",
    "distribute", "fit-within-slide",
}
ALIGNMENTS = {"left", "center-x", "right", "top", "center-y", "bottom"}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise BentoConverterError(message)


def _number(value: Any, name: str) -> float:
    _require(isinstance(value, (int, float)) and not isinstance(value, bool), f"{name} must be numeric")
    result = float(value)
    _require(math.isfinite(result), f"{name} must be finite")
    return result


def _strings(value: Any, name: str) -> list[str]:
    if isinstance(value, str):
        value = [value]
    _require(isinstance(value, list) and all(isinstance(item, str) and item for item in value),
             f"{name} must be a string or list of strings")
    return list(dict.fromkeys(value))


def validate_quick_edit_plan(value: Any) -> dict[str, Any]:
    _require(isinstance(value, dict), "Quick edit plan must be an object")
    _require(value.get("format") == QUICK_PLAN_FORMAT,
             f"Quick edit plan format must be {QUICK_PLAN_FORMAT!r}")
    slide = value.get("slide")
    _require(isinstance(slide, (str, int)) and not isinstance(slide, bool),
             "Quick edit plan requires a 1-based slide number or slide ID")
    if isinstance(slide, int):
        _require(slide >= 1, "Slide number must be 1 or greater")
    operations = value.get("operations")
    _require(isinstance(operations, list) and operations, "Quick edit plan requires operations")
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(operations):
        _require(isinstance(raw, dict), f"operations[{index}] must be an object")
        name = raw.get("op")
        _require(name in QUICK_OPERATIONS, f"operations[{index}].op is unsupported: {name!r}")
        operation = dict(raw)
        target = operation.get("target", "body")
        if isinstance(target, str):
            _require(target in GROUPS, f"operations[{index}].target is unsupported: {target!r}")
            target = {"group": target}
        else:
            _require(isinstance(target, dict), f"operations[{index}].target must be a group or object")
            target = dict(target)
            group = target.get("group", "body")
            _require(group in GROUPS, f"operations[{index}].target.group is unsupported: {group!r}")
            target["group"] = group
            for field in ("elementIds", "idContains", "types", "excludeIds"):
                if field in target:
                    target[field] = _strings(target[field], f"operations[{index}].target.{field}")
        operation["target"] = target
        if name == "shift":
            operation["dx"] = _number(operation.get("dx", 0), f"operations[{index}].dx")
            operation["dy"] = _number(operation.get("dy", 0), f"operations[{index}].dy")
            _require(operation["dx"] or operation["dy"], f"operations[{index}] shift must move something")
        elif name in {"text-style", "shape-style"}:
            properties = operation.get("properties")
            _require(isinstance(properties, dict) and properties,
                     f"operations[{index}].properties must be a non-empty object")
        elif name == "resize-group":
            operation["scaleX"] = _number(operation.get("scaleX", 1), f"operations[{index}].scaleX")
            operation["scaleY"] = _number(operation.get("scaleY", 1), f"operations[{index}].scaleY")
            _require(operation["scaleX"] > 0 and operation["scaleY"] > 0,
                     f"operations[{index}] scale factors must be positive")
            anchor = operation.get("anchor", "center")
            _require(anchor in {"center", "top-left"}, f"operations[{index}].anchor is unsupported")
            operation["anchor"] = anchor
        elif name == "align":
            _require(operation.get("alignment") in ALIGNMENTS,
                     f"operations[{index}].alignment is unsupported")
        elif name == "distribute":
            _require(operation.get("axis") in {"x", "y"}, f"operations[{index}].axis must be x or y")
        elif name == "fit-within-slide":
            operation["margin"] = _number(operation.get("margin", 0), f"operations[{index}].margin")
            _require(operation["margin"] >= 0, f"operations[{index}].margin must not be negative")
            operation["allowScale"] = bool(operation.get("allowScale", False))
        normalized.append(operation)
    result = dict(value)
    result["operations"] = normalized
    expected = result.get("expectedDocumentRevision")
    _require(expected is None or (isinstance(expected, str) and expected.startswith("sha256:")),
             "expectedDocumentRevision must be a SHA-256 revision")
    return result


def load_quick_edit_plan(path: str | Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BentoConverterError(f"Could not read quick edit plan: {exc}") from exc
    return validate_quick_edit_plan(value)


def _slide_size(document: dict[str, Any]) -> tuple[float, float]:
    size = document.get("size", {})
    width = _number(size.get("width", 1280), "document.size.width")
    height = _number(size.get("height", 720), "document.size.height")
    _require(width > 0 and height > 0, "Document size must be positive")
    return width, height


def _resolve_slide(document: dict[str, Any], selector: str | int) -> tuple[int, dict[str, Any]]:
    slides = document.get("slides", [])
    if isinstance(selector, int):
        _require(selector <= len(slides), f"Slide {selector} does not exist; deck has {len(slides)} slides")
        return selector - 1, slides[selector - 1]
    exact = [(index, slide) for index, slide in enumerate(slides) if slide.get("id") == selector]
    _require(len(exact) == 1, f"Bento slide does not exist or is ambiguous: {selector!r}")
    return exact[0]


def _bounds(element: dict[str, Any]) -> tuple[float, float, float, float]:
    return tuple(_number(element.get(field, 0), f"element.{field}") for field in ("x", "y", "w", "h"))


def _roles(element: dict[str, Any], width: float, height: float) -> set[str]:
    element_id = str(element.get("id", "")).lower()
    element_type = str(element.get("type", ""))
    x, y, w, h = _bounds(element)
    roles: set[str] = set()
    background_tokens = ("background", "backdrop", "canvas")
    header_tokens = ("title", "kicker", "header", "eyebrow")
    footer_tokens = ("footer", "page-number", "pagenumber")
    if (any(token in element_id for token in background_tokens)
            or (x <= width * .02 and y <= height * .02 and w >= width * .85 and h >= height * .85)):
        roles.add("background")
    if any(token in element_id for token in footer_tokens) or y >= height * .92:
        roles.add("footer")
    if any(token in element_id for token in header_tokens) and y < height * .25:
        roles.add("header")
    if "title" in element_id and y < height * .25:
        roles.add("title")
    if not roles.intersection({"background", "footer", "header"}):
        roles.add("body")
    if element_type == "text":
        roles.add("text")
    if element_type == "shape":
        roles.add("shape")
    return roles


def _matches_target(element: dict[str, Any], target: dict[str, Any], width: float, height: float) -> bool:
    group = target.get("group", "body")
    roles = _roles(element, width, height)
    if group == "all-elements":
        matched = True
    elif group == "all":
        matched = "background" not in roles
    elif group == "body":
        matched = "body" in roles
    elif group == "header":
        matched = "header" in roles
    elif group == "footer":
        matched = "footer" in roles
    elif group == "title":
        matched = "title" in roles
    elif group == "text":
        matched = "text" in roles and "background" not in roles
    elif group == "shapes":
        matched = "shape" in roles and "background" not in roles
    elif group == "body-text":
        matched = "body" in roles and "text" in roles
    else:
        matched = "body" in roles and "shape" in roles
    element_id = str(element.get("id", ""))
    if target.get("elementIds"):
        matched = element_id in target["elementIds"]
    if target.get("idContains"):
        lowered = element_id.lower()
        matched = matched and any(token.lower() in lowered for token in target["idContains"])
    if target.get("types"):
        matched = matched and element.get("type") in target["types"]
    if element_id in target.get("excludeIds", []):
        matched = False
    return matched


def _select(slide: dict[str, Any], target: dict[str, Any], width: float, height: float) -> list[dict[str, Any]]:
    result = [element for element in slide.get("elements", []) if _matches_target(element, target, width, height)]
    _require(result, f"Quick edit target selected no elements on slide {slide.get('id')!r}")
    if target.get("elementIds"):
        found = {element.get("id") for element in result}
        missing = sorted(set(target["elementIds"]) - found)
        _require(not missing, f"Quick edit target element IDs are missing: {missing!r}")
    return result


def _group_bounds(elements: list[dict[str, Any]]) -> tuple[float, float, float, float]:
    frames = [_bounds(element) for element in elements]
    left = min(x for x, _, _, _ in frames)
    top = min(y for _, y, _, _ in frames)
    right = max(x + w for x, _, w, _ in frames)
    bottom = max(y + h for _, y, _, h in frames)
    return left, top, right, bottom


def _geometry_operation(slide_id: str, element: dict[str, Any], properties: dict[str, Any]) -> dict[str, Any]:
    element_type = element.get("type")
    if element_type == "shape":
        op = "set-shape"
    elif element_type == "text":
        op = "set-text-style"
    else:
        op = "set-geometry"
    return {"op": op, "slideId": slide_id, "elementId": element["id"], "properties": properties}


def compile_quick_edit_plan(document: dict[str, Any], plan: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compile semantic operations without mutating the current document."""

    plan = validate_quick_edit_plan(plan)
    validate_bento_doc(document)
    width, height = _slide_size(document)
    slide_number, slide = _resolve_slide(document, plan["slide"])
    working = copy.deepcopy(slide)
    primitive: list[dict[str, Any]] = []
    resolved: list[dict[str, Any]] = []

    for index, operation in enumerate(plan["operations"]):
        targets = _select(working, operation["target"], width, height)
        before = _group_bounds(targets)
        name = operation["op"]
        generated: list[dict[str, Any]] = []
        if name == "shift":
            for element in targets:
                x, y, _, _ = _bounds(element)
                properties = {"x": x + operation["dx"], "y": y + operation["dy"]}
                element.update(properties)
                generated.append(_geometry_operation(slide["id"], element, properties))
        elif name == "text-style":
            text_targets = [element for element in targets if element.get("type") == "text"]
            _require(text_targets, f"operations[{index}] selected no text elements")
            for element in text_targets:
                generated.append({
                    "op": "set-text-style", "slideId": slide["id"], "elementId": element["id"],
                    "properties": operation["properties"],
                })
                element.update(operation["properties"])
            targets = text_targets
        elif name == "shape-style":
            shape_targets = [element for element in targets if element.get("type") == "shape"]
            _require(shape_targets, f"operations[{index}] selected no shape elements")
            for element in shape_targets:
                generated.append({
                    "op": "set-shape", "slideId": slide["id"], "elementId": element["id"],
                    "properties": operation["properties"],
                })
                element.update(operation["properties"])
            targets = shape_targets
        elif name == "resize-group":
            left, top, right, bottom = before
            anchor_x = left if operation["anchor"] == "top-left" else (left + right) / 2
            anchor_y = top if operation["anchor"] == "top-left" else (top + bottom) / 2
            for element in targets:
                x, y, w, h = _bounds(element)
                properties = {
                    "x": anchor_x + (x - anchor_x) * operation["scaleX"],
                    "y": anchor_y + (y - anchor_y) * operation["scaleY"],
                    "w": w * operation["scaleX"], "h": h * operation["scaleY"],
                }
                element.update(properties)
                generated.append(_geometry_operation(slide["id"], element, properties))
        elif name == "align":
            left, top, right, bottom = before
            for element in targets:
                x, y, w, h = _bounds(element)
                alignment = operation["alignment"]
                properties: dict[str, Any] = {}
                if alignment == "left": properties["x"] = left
                elif alignment == "center-x": properties["x"] = (left + right - w) / 2
                elif alignment == "right": properties["x"] = right - w
                elif alignment == "top": properties["y"] = top
                elif alignment == "center-y": properties["y"] = (top + bottom - h) / 2
                else: properties["y"] = bottom - h
                element.update(properties)
                generated.append(_geometry_operation(slide["id"], element, properties))
        elif name == "distribute":
            _require(len(targets) >= 3, f"operations[{index}] distribute requires at least three elements")
            axis = operation["axis"]
            ordered = sorted(targets, key=lambda element: _bounds(element)[0 if axis == "x" else 1])
            positions = [_bounds(element) for element in ordered]
            start = positions[0][0 if axis == "x" else 1]
            end = positions[-1][0 if axis == "x" else 1]
            sizes = [frame[2 if axis == "x" else 3] for frame in positions]
            available = end + sizes[-1] - start - sum(sizes)
            gap = available / (len(ordered) - 1)
            cursor = start
            for element, size in zip(ordered, sizes):
                properties = {axis: cursor}
                element.update(properties)
                generated.append(_geometry_operation(slide["id"], element, properties))
                cursor += size + gap
        else:
            margin = operation["margin"]
            left, top, right, bottom = before
            group_width, group_height = right - left, bottom - top
            available_width, available_height = width - 2 * margin, height - 2 * margin
            scale = 1.0
            if group_width > available_width or group_height > available_height:
                _require(operation["allowScale"], f"operations[{index}] target is too large to fit without scaling")
                scale = min(available_width / group_width, available_height / group_height)
            target_left = min(max(left, margin), width - margin - group_width * scale)
            target_top = min(max(top, margin), height - margin - group_height * scale)
            for element in targets:
                x, y, w, h = _bounds(element)
                properties = {
                    "x": target_left + (x - left) * scale, "y": target_top + (y - top) * scale,
                    "w": w * scale, "h": h * scale,
                }
                element.update(properties)
                generated.append(_geometry_operation(slide["id"], element, properties))
        after = _group_bounds(targets)
        primitive.extend(generated)
        resolved.append({
            "index": index, "op": name, "target": operation["target"],
            "elementIds": [element["id"] for element in targets],
            "beforeBounds": {"left": before[0], "top": before[1], "right": before[2], "bottom": before[3]},
            "afterBounds": {"left": after[0], "top": after[1], "right": after[2], "bottom": after[3]},
            "generatedOperationCount": len(generated),
        })

    for element in working.get("elements", []):
        if element.get("id") not in {item_id for item in resolved for item_id in item["elementIds"]}:
            continue
        x, y, w, h = _bounds(element)
        _require(x >= 0 and y >= 0 and x + w <= width and y + h <= height,
                 f"Quick edit would place element {element.get('id')!r} outside the slide")
    primitive_plan = {
        "format": PLAN_FORMAT,
        "operations": primitive,
    }
    if plan.get("expectedDocumentRevision"):
        primitive_plan["expectedDocumentRevision"] = plan["expectedDocumentRevision"]
    primitive_plan = validate_browser_edit_plan(primitive_plan)
    return primitive_plan, {
        "format": QUICK_REPORT_FORMAT, "slideNumber": slide_number + 1,
        "slideId": slide["id"], "canvas": {"width": width, "height": height},
        "resolvedOperations": resolved, "boundsCheck": "pass",
        "primitiveOperationCount": len(primitive),
    }


def run_quick_edit(
    editor_url: str, plan: dict[str, Any], *, dry_run: bool = False,
    screenshots_dir: str | Path | None = None, browser_executable: str | Path | None = None,
) -> dict[str, Any]:
    """Resolve a current editor snapshot, compile once, and save only the selected slide."""

    plan = validate_quick_edit_plan(plan)
    parsed = urlparse(editor_url)
    _require(parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"},
             "Quick editing requires a loopback http:// Work editor URL")
    _require(parsed.username is None and parsed.password is None, "Editor URL must not contain credentials")
    client = WorkEditorClient(editor_url.rstrip("/"), {})
    status = client.get("/api/status")
    snapshot = client.get("/api/document")
    _require(snapshot.get("documentRevision") == status.get("documentRevision"),
             "Work editor changed while preparing the quick edit; retry against the latest revision")
    expected = plan.get("expectedDocumentRevision")
    _require(expected is None or expected == status.get("documentRevision"),
             "Quick edit plan is stale; reload the current Work editor revision")
    document = snapshot.get("document")
    _require(isinstance(document, dict), "Work editor document snapshot is missing")
    bound_plan = dict(plan)
    bound_plan["expectedDocumentRevision"] = status.get("documentRevision")
    primitive, compilation = compile_quick_edit_plan(document, bound_plan)
    if dry_run:
        return {
            **compilation, "editorUrl": editor_url, "dryRun": True,
            "documentRevision": status.get("documentRevision"), "saveStatus": "not-run",
            "primitivePlan": primitive,
        }
    try:
        report = run_browser_edit(
            editor_url, primitive, screenshots_dir=screenshots_dir,
            browser_executable=browser_executable,
        )
    except BrowserEditPostSaveError as exc:
        exc.report["quickEdit"] = compilation
        raise BrowserEditPostSaveError(str(exc), exc.report) from exc
    report["quickEdit"] = compilation
    return report
