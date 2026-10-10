"""Reusable, revision-checked Bento editing through the fastest safe editor path."""

from __future__ import annotations

import copy
import hashlib
import html as html_module
import json
import math
import re
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .bento_validator import validate_bento_doc
from .browser_harness import BrowserHarness
from .errors import BentoConverterError
from .html_document import embed_bento_doc, extract_bento_doc
from .html_converter import sanitize_inline_html
from .work_editor_client import WorkEditorClient


PLAN_FORMAT = "bento/browser-edit-plan/v1"
REPORT_FORMAT = "bento/browser-edit-report/v1"
HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


class BrowserEditPostSaveError(BentoConverterError):
    """The edit committed, but its post-save browser evidence did not finish."""

    def __init__(self, message: str, report: dict[str, Any]):
        self.report = report
        super().__init__(f"Bento edit was saved, but post-save validation failed: {message}")

CAPABILITIES: dict[str, dict[str, Any]] = {
    "set-shape": {
        "modes": ["authoring", "final"],
        "properties": [
            "fill", "stroke", "lineWidth", "radius", "x", "y", "w", "h",
            "rotation", "opacity", "shadow",
        ],
    },
    "set-text-style": {
        "modes": ["authoring", "final"],
        "properties": [
            "color", "font", "fontSizePt", "fontWeight", "align", "verticalAlign",
            "lineHeight", "outline", "x", "y", "w", "h", "rotation", "opacity",
            "shadow",
        ],
    },
    "set-geometry": {
        "modes": ["authoring"],
        "properties": ["x", "y", "w", "h"],
    },
    "set-text": {"modes": ["authoring"], "plainTextOnly": True},
    "set-text-html": {"modes": ["authoring"], "sanitizedInlineHtmlOnly": True},
    "set-order": {
        "modes": ["authoring", "final"],
        "orders": ["front", "forward", "backward", "back"],
    },
    "add-rectangle": {
        "modes": ["authoring"],
        "orders": ["front", "back", "background-card"],
    },
    "duplicate": {"modes": ["authoring"]},
    "delete": {"modes": ["authoring"]},
    "align": {
        "modes": ["authoring", "final"],
        "alignments": ["left", "center-x", "right", "top", "center-y", "bottom"],
    },
}

COMMON_NUMBERS = {
    "x": ("X",),
    "y": ("Y",),
    "w": ("W",),
    "h": ("H",),
    "rotation": ("角度", "Angle"),
    "opacity": ("不透明度", "Opacity"),
}
SHAPE_NUMBERS = {
    "lineWidth": ("線幅", "Stroke width"),
    "radius": ("角丸", "Corner radius"),
    **COMMON_NUMBERS,
}
TEXT_NUMBERS = {
    "fontSizePt": ("サイズ (pt)", "Size (pt)"),
    "lineHeight": ("行間", "Line height"),
    "outline": ("縁取り", "Outline"),
    **COMMON_NUMBERS,
}
ORDER_BUTTONS = {"front": "⇈", "forward": "↑", "backward": "↓", "back": "⇊"}
ALIGNMENTS = {"left", "center-x", "right", "top", "center-y", "bottom"}


def _digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise BentoConverterError(message)


def _loopback_editor_url(value: str) -> str:
    parsed = urlparse(value)
    _require(parsed.scheme == "http", "Bento browser editing requires a loopback http:// URL")
    _require(
        parsed.hostname in {"127.0.0.1", "localhost", "::1"},
        "Bento browser editing refuses non-loopback editor URLs",
    )
    _require(parsed.username is None and parsed.password is None, "Editor URL must not contain credentials")
    return value.rstrip("/") + "/"


def _number(value: Any, *, name: str) -> float:
    _require(isinstance(value, (int, float)) and not isinstance(value, bool), f"{name} must be numeric")
    result = float(value)
    _require(math.isfinite(result), f"{name} must be finite")
    return result


def _color(value: Any, *, name: str) -> str:
    _require(isinstance(value, str) and HEX_COLOR.fullmatch(value) is not None, f"{name} must be #RRGGBB")
    return value.upper()


def validate_browser_edit_plan(value: Any) -> dict[str, Any]:
    _require(isinstance(value, dict), "Browser edit plan must be an object")
    _require(value.get("format") == PLAN_FORMAT, f"Browser edit plan format must be {PLAN_FORMAT!r}")
    operations = value.get("operations")
    _require(isinstance(operations, list) and operations, "Browser edit plan requires operations")
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(operations):
        _require(isinstance(raw, dict), f"operations[{index}] must be an object")
        name = raw.get("op")
        _require(name in CAPABILITIES, f"operations[{index}].op is unsupported: {name!r}")
        slide_id = raw.get("slideId")
        _require(isinstance(slide_id, str) and slide_id, f"operations[{index}].slideId is required")
        operation = dict(raw)
        if name in {"set-shape", "set-text-style", "set-geometry", "set-order", "set-text", "set-text-html", "duplicate", "delete"}:
            element_id = raw.get("elementId")
            _require(isinstance(element_id, str) and element_id, f"operations[{index}].elementId is required")
        if name == "align":
            element_ids = raw.get("elementIds")
            _require(
                isinstance(element_ids, list) and len(element_ids) >= 2
                and all(isinstance(item, str) and item for item in element_ids),
                f"operations[{index}].elementIds requires at least two IDs",
            )
            _require(raw.get("alignment") in ALIGNMENTS, f"operations[{index}].alignment is unsupported")
        if name in {"set-shape", "set-text-style", "set-geometry", "add-rectangle"}:
            properties = raw.get("properties", {})
            _require(isinstance(properties, dict), f"operations[{index}].properties must be an object")
            capability = "set-shape" if name == "add-rectangle" else name
            allowed = set(CAPABILITIES[capability]["properties"])
            unknown = sorted(set(properties) - allowed)
            _require(not unknown, f"operations[{index}] has unsupported properties: {unknown!r}")
            for field, item in properties.items():
                if field in {"fill", "stroke", "color"}:
                    properties[field] = _color(item, name=f"operations[{index}].properties.{field}")
                elif field in set(SHAPE_NUMBERS) | set(TEXT_NUMBERS):
                    properties[field] = _number(item, name=f"operations[{index}].properties.{field}")
            operation["properties"] = properties
        if name == "set-text":
            _require(isinstance(raw.get("text"), str), f"operations[{index}].text must be a string")
        if name == "set-text-html":
            _require(isinstance(raw.get("html"), str), f"operations[{index}].html must be a string")
            sanitized = sanitize_inline_html(raw["html"])
            _require(sanitized, f"operations[{index}].html must contain visible inline content")
            operation["html"] = sanitized
        if name in {"set-order", "add-rectangle"}:
            order = raw.get("order", "front")
            allowed_orders = CAPABILITIES[name]["orders"]
            _require(order in allowed_orders, f"operations[{index}].order is unsupported")
            operation["order"] = order
        normalized.append(operation)
    result = dict(value)
    result["operations"] = normalized
    expected = result.get("expectedDocumentRevision")
    _require(expected is None or (isinstance(expected, str) and expected.startswith("sha256:")), "expectedDocumentRevision must be a SHA-256 revision")
    return result


def load_browser_edit_plan(path: str | Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BentoConverterError(f"Could not read browser edit plan: {exc}") from exc
    return validate_browser_edit_plan(value)


def _sidecar_path(html_path: Path) -> Path:
    name = html_path.name
    if name.endswith(".bento.html"):
        return html_path.with_name(name[: -len(".bento.html")] + ".bento.json")
    return html_path.with_suffix(".bento.json")


def _repository_path(repository: Path, value: str | Path, *, label: str) -> Path:
    path = Path(value)
    resolved = (path if path.is_absolute() else repository / path).resolve()
    try:
        resolved.relative_to(repository)
    except ValueError as exc:
        raise BentoConverterError(f"Work editor {label} escapes its repository: {resolved}") from exc
    return resolved


def validate_edit_report_path(
    editor_url: str,
    report_path: str | Path,
    *,
    input_paths: tuple[str | Path, ...] = (),
    allowed_formats: tuple[str, ...] = (REPORT_FORMAT,),
) -> Path:
    """Resolve a report path and reject collisions before any Bento mutation starts."""

    editor_url = _loopback_editor_url(editor_url)
    status = WorkEditorClient(editor_url.rstrip("/"), {}).get("/api/status")
    repository_value = status.get("repository")
    _require(isinstance(repository_value, str) and repository_value, "Work editor status has no repository")
    repository = Path(repository_value).resolve()
    resolved_report = _repository_path(repository, report_path, label="report path")

    reserved = {(repository / "deck.yaml").resolve()}
    for input_path in input_paths:
        reserved.add(Path(input_path).resolve())

    artifact_paths = status.get("artifactPaths")
    _require(
        isinstance(artifact_paths, list) and artifact_paths
        and all(isinstance(path, str) and path for path in artifact_paths),
        "Work editor status does not expose its transaction artifacts",
    )
    artifacts = {
        _repository_path(repository, path, label="artifact path")
        for path in artifact_paths
    }
    reserved.update(artifacts)

    target_value = status.get("target")
    _require(isinstance(target_value, str) and target_value, "Work editor status has no target")
    target_reference = Path(target_value)
    target_candidates = [
        path for path in artifacts
        if path.name == target_reference.name
        and (
            target_reference.name == target_value
            or path.relative_to(repository).as_posix() == target_reference.as_posix()
        )
    ]
    _require(len(target_candidates) == 1, "Work editor target does not match exactly one transaction artifact")
    target = target_candidates[0]
    reserved.update({
        target,
        _sidecar_path(target),
        target.parent / "save-report.json",
        target.parent / "authoring-save-report.json",
    })
    if resolved_report in reserved:
        raise BentoConverterError(
            f"Edit report must not overwrite an input, workflow state, or Bento artifact: {resolved_report}"
        )
    revisions = (target.parent / "revisions").resolve()
    try:
        resolved_report.relative_to(revisions)
    except ValueError:
        pass
    else:
        raise BentoConverterError(
            f"Edit report must stay outside the immutable revisions directory: {resolved_report}"
        )

    if resolved_report.exists():
        try:
            existing = json.loads(resolved_report.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BentoConverterError(f"Edit report refuses to overwrite a non-report file: {resolved_report}") from exc
        if not isinstance(existing, dict) or existing.get("format") not in set(allowed_formats):
            raise BentoConverterError(f"Edit report refuses to overwrite a different report format: {resolved_report}")
    return resolved_report


def _element(slide: dict[str, Any], element_id: str) -> dict[str, Any]:
    matches = [element for element in slide.get("elements", []) if element.get("id") == element_id]
    _require(len(matches) == 1, f"Bento element does not exist on slide {slide['id']!r}: {element_id!r}")
    return matches[0]


def _unique_element_id(slide: dict[str, Any], base: str) -> str:
    existing = {element.get("id") for element in slide.get("elements", [])}
    candidate = base
    while candidate in existing:
        candidate = f"{base}-{uuid.uuid4().hex[:8]}"
    return candidate


def _move_element(elements: list[dict[str, Any]], element: dict[str, Any], order: str) -> None:
    index = elements.index(element)
    if order == "front":
        target = len(elements) - 1
    elif order == "forward":
        target = min(len(elements) - 1, index + 1)
    elif order == "backward":
        target = max(0, index - 1)
    else:
        target = 0
    if target != index:
        elements.pop(index)
        elements.insert(target, element)


def _apply_authoring_plan(
    document: dict[str, Any], plan: dict[str, Any],
) -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    """Apply supported authoring operations without depending on UI control events."""

    proposed = copy.deepcopy(document)
    slides = {slide.get("id"): slide for slide in proposed.get("slides", [])}
    requested_slide_ids: list[str] = []
    results: list[dict[str, Any]] = []
    for index, operation in enumerate(plan["operations"]):
        name = operation["op"]
        _require("authoring" in CAPABILITIES[name]["modes"], f"{name!r} is not allowed in authoring mode")
        slide_id = operation["slideId"]
        _require(slide_id in slides, f"Bento slide does not exist: {slide_id!r}")
        slide = slides[slide_id]
        elements = slide["elements"]
        if slide_id not in requested_slide_ids:
            requested_slide_ids.append(slide_id)
        result: dict[str, Any] = {"index": index, "op": name, "slideId": slide_id}

        if name == "add-rectangle":
            properties = operation.get("properties", {})
            new_element = {
                "id": _unique_element_id(slide, "browser-edit-rectangle"),
                "type": "shape", "shape": "rect",
                "x": properties.get("x", 100), "y": properties.get("y", 100),
                "w": properties.get("w", 320), "h": properties.get("h", 180),
                "rotation": properties.get("rotation", 0), "opacity": properties.get("opacity", 1),
                "fill": properties.get("fill", "#DBEAFE"),
                "stroke": properties.get("stroke", "#1D4ED8"),
                "strokeWidth": properties.get("lineWidth", 1),
                "radius": properties.get("radius", 0),
            }
            if "shadow" in properties:
                new_element["shadow"] = properties["shadow"]
            elements.append(new_element)
            order = operation["order"]
            if order == "background-card":
                elements.pop()
                elements.insert(min(1, len(elements)), new_element)
            elif order != "front":
                _move_element(elements, new_element, order)
            result["elementId"] = new_element["id"]
        elif name == "align":
            targets = [_element(slide, element_id) for element_id in operation["elementIds"]]
            _require(
                all(target.get("type") in {"shape", "text"} for target in targets),
                "align currently supports editable shape and text elements",
            )
            left = min(target["x"] for target in targets)
            right = max(target["x"] + target["w"] for target in targets)
            top = min(target["y"] for target in targets)
            bottom = max(target["y"] + target["h"] for target in targets)
            alignment = operation["alignment"]
            for target in targets:
                if alignment == "left": target["x"] = left
                elif alignment == "center-x": target["x"] = (left + right - target["w"]) / 2
                elif alignment == "right": target["x"] = right - target["w"]
                elif alignment == "top": target["y"] = top
                elif alignment == "center-y": target["y"] = (top + bottom - target["h"]) / 2
                else: target["y"] = bottom - target["h"]
            result["elementIds"] = operation["elementIds"]
        else:
            target = _element(slide, operation["elementId"])
            result["elementId"] = operation["elementId"]
            if name == "set-shape":
                _require(target.get("type") == "shape", "set-shape requires a shape element")
                mapping = {"lineWidth": "strokeWidth"}
                for field, value in operation.get("properties", {}).items():
                    target[mapping.get(field, field)] = value
            elif name == "set-text-style":
                _require(target.get("type") == "text", "set-text-style requires a text element")
                mapping = {
                    "font": "fontFamily", "fontSizePt": "fontSize",
                    "verticalAlign": "valign", "outline": "textStroke",
                }
                for field, value in operation.get("properties", {}).items():
                    if field == "fontSizePt":
                        value = round(value * 96 / 72, 2)
                    target[mapping.get(field, field)] = value
            elif name == "set-geometry":
                for field, value in operation.get("properties", {}).items():
                    target[field] = value
            elif name == "set-text":
                _require(target.get("type") == "text", "set-text requires a text element")
                _require(
                    not target.get("equationId") and not target.get("latexSource")
                    and not str(target.get("html", "")).startswith("$$"),
                    "set-text refuses equations; use the registered equation authoring route",
                )
                target["html"] = html_module.escape(operation["text"])
            elif name == "set-text-html":
                _require(target.get("type") == "text", "set-text-html requires a text element")
                _require(
                    not target.get("equationId") and not target.get("latexSource")
                    and not str(target.get("html", "")).startswith("$$"),
                    "set-text-html refuses equations; use the registered equation authoring route",
                )
                target["html"] = operation["html"]
            elif name == "set-order":
                _move_element(elements, target, operation["order"])
            elif name == "duplicate":
                duplicate = copy.deepcopy(target)
                duplicate["id"] = _unique_element_id(slide, f"{target['id']}-copy")
                duplicate["x"] = duplicate.get("x", 0) + 16
                duplicate["y"] = duplicate.get("y", 0) + 16
                elements.insert(elements.index(target) + 1, duplicate)
                result["newElementId"] = duplicate["id"]
            elif name == "delete":
                elements.remove(target)
        results.append(result)
    validate_bento_doc(proposed)
    return proposed, requested_slide_ids, results


def _field_by_parent_text(panel: Any, selector: str, labels: str | tuple[str, ...]) -> Any:
    accepted = (labels,) if isinstance(labels, str) else labels
    matches = [
        field for field in panel.locator(selector).all()
        if field.evaluate("element => element.parentElement.textContent.trim()") in accepted
    ]
    _require(len(matches) == 1, f"Bento property {accepted!r} is unavailable or ambiguous")
    return matches[0]


def _set_number(panel: Any, labels: str | tuple[str, ...], value: float) -> None:
    field = _field_by_parent_text(panel, 'input[type="number"]', labels)
    field.fill(str(value))
    field.press("Tab")


def _set_select(panel: Any, labels: str | tuple[str, ...], value: Any) -> None:
    field = _field_by_parent_text(panel, "select", labels)
    field.select_option(str(value))


def _selection(page: Any) -> list[str]:
    return page.evaluate("Array.from(window.bento.selection || [])")


def _activate_slide(page: Any, slide_id: str, harness: BrowserHarness) -> tuple[int, dict[str, Any]]:
    slides = page.evaluate("window.bento.doc.slides")
    indexes = [index for index, slide in enumerate(slides) if slide.get("id") == slide_id]
    _require(len(indexes) == 1, f"Bento slide does not exist: {slide_id!r}")
    index = indexes[0]
    page.locator(".ed-thumb").nth(index).click()
    page.wait_for_function(
        "expected => document.querySelector('.ed-stage .bento-slide')?.dataset.slideId === expected",
        arg=slide_id,
    )
    harness.settle(page)
    return index, slides[index]


def _select_element(page: Any, slide: dict[str, Any], element_id: str, harness: BrowserHarness) -> dict[str, Any]:
    elements = [element for element in slide["elements"] if element.get("id") == element_id]
    _require(len(elements) == 1, f"Bento element does not exist on slide {slide['id']!r}: {element_id!r}")
    target = page.locator(f'.ed-stage [data-el-id="{element_id}"]')
    _require(target.count() == 1, f"Rendered Bento element is missing: {element_id!r}")
    box = target.bounding_box()
    _require(box is not None and box["width"] > 0 and box["height"] > 0, f"Bento element has no visible bounds: {element_id!r}")
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    page.mouse.click(x, y)
    for _ in range(len(slide["elements"]) + 2):
        if _selection(page) == [element_id]:
            harness.settle(page)
            return elements[0]
        page.keyboard.down("Alt")
        page.mouse.click(x, y)
        page.keyboard.up("Alt")
    raise BentoConverterError(
        f"Alt+click overlap selection could not select {element_id!r}; selection={_selection(page)!r}"
    )


def _panel(page: Any, titles: tuple[str, ...]) -> Any:
    panel = page.locator(".ed-props")
    page.wait_for_function(
        "expected => { const text=(document.querySelector('.ed-props')?.innerText || '').toLowerCase(); "
        "return expected.some(title => text.startsWith(title.toLowerCase())); }",
        arg=list(titles),
    )
    return panel


def _named_button(page: Any, names: tuple[str, ...]) -> Any:
    for name in names:
        button = page.get_by_role("button", name=name, exact=True)
        if button.count() == 1:
            return button
    raise BentoConverterError(f"Bento button is unavailable: {names!r}")


def _set_shape_properties(page: Any, properties: dict[str, Any]) -> None:
    panel = _panel(page, ("図形", "SHAPE"))
    colors = panel.locator('input[type="color"]')
    if "fill" in properties:
        _require(colors.count() >= 1, "Bento shape fill control is unavailable")
        colors.nth(0).fill(properties["fill"])
        colors.nth(0).press("Tab")
    if "stroke" in properties:
        _require(colors.count() >= 2, "Bento shape stroke control is unavailable")
        colors.nth(1).fill(properties["stroke"])
        colors.nth(1).press("Tab")
    for field, label in SHAPE_NUMBERS.items():
        if field in properties:
            _set_number(panel, label, properties[field])
    if "shadow" in properties:
        _set_select(panel, ("シャドウ", "Shadow"), properties["shadow"])


def _set_text_style(page: Any, properties: dict[str, Any]) -> None:
    panel = _panel(page, ("テキスト", "TEXT"))
    if "color" in properties:
        color = _field_by_parent_text(panel, 'input[type="color"]', ("色", "Color", "Colour"))
        color.fill(properties["color"])
        color.press("Tab")
    for field, label in TEXT_NUMBERS.items():
        if field in properties:
            _set_number(panel, label, properties[field])
    for field, label in {
        "font": ("フォント", "Font"),
        "fontWeight": ("ウェイト", "Weight"),
        "align": ("整列", "Align"),
        "verticalAlign": ("垂直揃え", "Vertical align"),
        "shadow": ("シャドウ", "Shadow"),
    }.items():
        if field in properties:
            _set_select(panel, label, properties[field])


def _order(page: Any, value: str) -> None:
    panel = page.locator(".ed-props")
    panel.get_by_role("button", name=ORDER_BUTTONS[value], exact=True).click()


def _capture_slide(page: Any, slide_id: str, output: Path, harness: BrowserHarness) -> None:
    _activate_slide(page, slide_id, harness)
    page.evaluate(
        """
        () => {
          document.querySelector('#bento-browser-edit-screenshot')?.remove();
          const source = document.querySelector('.ed-stage .bento-slide');
          const clone = source.cloneNode(true);
          clone.id = 'bento-browser-edit-screenshot';
          Object.assign(clone.style, {
            position: 'fixed', left: '0', top: '0', margin: '0',
            transform: 'none', zIndex: '2147483647'
          });
          document.body.appendChild(clone);
        }
        """
    )
    harness.settle(page)
    output.parent.mkdir(parents=True, exist_ok=True)
    page.locator("#bento-browser-edit-screenshot").screenshot(path=str(output))
    page.evaluate("document.querySelector('#bento-browser-edit-screenshot')?.remove()")


def _run_authoring_api_edit(
    editor_url: str, plan: dict[str, Any], status_before: dict[str, Any], *,
    screenshots_dir: str | Path | None, browser_executable: str | Path | None,
    harness: BrowserHarness | None,
) -> dict[str, Any]:
    client = WorkEditorClient(editor_url, status_before)
    expected_revision = plan.get("expectedDocumentRevision")
    if expected_revision is not None:
        _require(
            expected_revision == status_before.get("documentRevision"),
            "Browser edit plan is stale; reload the current Work editor revision",
        )
    before_html = client.get_text("/")
    before_document = extract_bento_doc(before_html)
    validate_bento_doc(before_document)
    proposed_document, requested_slide_ids, operation_results = _apply_authoring_plan(
        before_document, plan,
    )
    before_hashes = {slide["id"]: _digest(slide) for slide in before_document["slides"]}
    proposed_hashes = {slide["id"]: _digest(slide) for slide in proposed_document["slides"]}
    changed_slide_ids = [
        slide_id for slide_id in before_hashes
        if before_hashes[slide_id] != proposed_hashes.get(slide_id)
    ]
    unexpected = [slide_id for slide_id in changed_slide_ids if slide_id not in requested_slide_ids]
    _require(not unexpected, f"Direct authoring edit changed unrequested slides: {unexpected!r}")
    if not changed_slide_ids:
        _require(status_before.get("validation") == "pass", "Current Work editor document is not valid")
        return {
            "format": REPORT_FORMAT, "editorUrl": editor_url, "editingMode": "authoring",
            "executionPath": "authoring-api",
            "beforeDocumentRevision": status_before.get("documentRevision"),
            "afterDocumentRevision": status_before.get("documentRevision"),
            "registryRevisionUnchanged": True, "requestedSlideIds": requested_slide_ids,
            "changedSlideIds": [], "operations": operation_results, "saveStatus": "no-op",
            "saveMessage": "No document changes; save skipped", "validation": "pass",
            "validationScope": "changed-slides", "fullDeckBrowserCheck": "deferred",
            "screenshots": [],
        }

    if harness is not None:
        harness.assert_no_blocked_network("bentoBrowserEdit")
    save_result = client.post("/api/save", {
        "baseDocumentRevision": status_before.get("documentRevision"),
        "baseRegistryRevision": status_before.get("registryRevision"),
        "serializedHtml": embed_bento_doc(before_html, proposed_document),
        "scopedSlideIds": requested_slide_ids,
        "operation": "browser-edit",
        "operationReport": {
            "format": REPORT_FORMAT, "executionPath": "authoring-api",
            "requestedSlideIds": requested_slide_ids, "operations": operation_results,
        },
    })
    save_message = (
        "変更なし" if save_result.get("noOp")
        else f"保存済み: {save_result.get('documentRevision', '')}"
    )
    status_after = client.get("/api/status")
    saved_changed_slide_ids = save_result.get("savedChangedSlideIds", changed_slide_ids)
    submitted_changed_slide_ids = save_result.get("submittedChangedSlideIds", changed_slide_ids)
    discarded_slide_ids = save_result.get("discardedSlideIds", [])
    discarded_document_fields = save_result.get("discardedDocumentFields", [])
    screenshots: list[str] = []
    try:
        _require(
            status_before.get("registryRevision") == status_after.get("registryRevision"),
            "Presentation-only authoring edit unexpectedly changed the registry",
        )
        _require(status_after.get("validation") == "pass", "Work editor did not validate the saved document")
        if not save_result.get("noOp"):
            _require(
                status_before.get("documentRevision") != status_after.get("documentRevision"),
                "Work editor did not advance the document revision after saving",
            )
        saved_document = extract_bento_doc(client.get_text("/?post-save-snapshot=1"))
        validate_bento_doc(saved_document)
        saved_hashes = {slide["id"]: _digest(slide) for slide in saved_document["slides"]}
        _require(
            all(saved_hashes.get(slide_id) == proposed_hashes.get(slide_id)
                for slide_id in saved_changed_slide_ids),
            "Saved scoped authoring edit differs from the reviewed slide proposal",
        )

        if screenshots_dir is not None:
            def capture(active_harness: BrowserHarness) -> None:
                with active_harness.page("bentoBrowserEdit") as page:
                    page.goto(editor_url, wait_until="load")
                    page.wait_for_function("window.bento && document.querySelectorAll('.ed-thumb').length > 0")
                    indexes = {slide["id"]: index for index, slide in enumerate(saved_document["slides"])}
                    for slide_id in saved_changed_slide_ids:
                        output = Path(screenshots_dir) / f"slide-{indexes[slide_id] + 1}.png"
                        _capture_slide(page, slide_id, output, active_harness)
                        screenshots.append(str(output.resolve()))
                    active_harness.assert_no_blocked_network("bentoBrowserEdit")

            if harness is not None:
                capture(harness)
            else:
                with BrowserHarness(browser_executable) as owned_harness:
                    capture(owned_harness)
        elif harness is not None:
            harness.assert_no_blocked_network("bentoBrowserEdit")
    except Exception as exc:
        report = {
            "format": REPORT_FORMAT, "editorUrl": editor_url, "editingMode": "authoring",
            "executionPath": "authoring-api",
            "beforeDocumentRevision": status_before.get("documentRevision"),
            "afterDocumentRevision": status_after.get("documentRevision"),
            "registryRevisionUnchanged": (
                status_before.get("registryRevision") == status_after.get("registryRevision")
            ),
            "requestedSlideIds": requested_slide_ids,
            "changedSlideIds": saved_changed_slide_ids,
            "submittedChangedSlideIds": submitted_changed_slide_ids,
            "discardedSlideIds": discarded_slide_ids,
            "discardedDocumentFields": discarded_document_fields,
            "operations": operation_results, "saveStatus": "committed",
            "saveMessage": save_message, "validation": "failed",
            "validationScope": "post-save", "postSaveError": str(exc),
            "fullDeckBrowserCheck": "deferred", "screenshots": screenshots,
        }
        raise BrowserEditPostSaveError(str(exc), report) from exc

    return {
        "format": REPORT_FORMAT, "editorUrl": editor_url, "editingMode": "authoring",
        "executionPath": "authoring-api",
        "beforeDocumentRevision": status_before.get("documentRevision"),
        "afterDocumentRevision": status_after.get("documentRevision"),
        "registryRevisionUnchanged": True, "requestedSlideIds": requested_slide_ids,
        "changedSlideIds": saved_changed_slide_ids,
        "submittedChangedSlideIds": submitted_changed_slide_ids,
        "discardedSlideIds": discarded_slide_ids,
        "discardedDocumentFields": discarded_document_fields,
        "operations": operation_results,
        "saveStatus": "no-op" if save_result.get("noOp") else "committed",
        "saveMessage": save_message, "validation": "pass",
        "validationScope": "changed-slides", "fullDeckBrowserCheck": "deferred",
        "screenshots": screenshots,
    }


def run_browser_edit(
    editor_url: str,
    plan: dict[str, Any],
    *,
    screenshots_dir: str | Path | None = None,
    browser_executable: str | Path | None = None,
    harness: BrowserHarness | None = None,
) -> dict[str, Any]:
    """Apply a validated edit plan through the fastest safe mode-specific route."""

    plan = validate_browser_edit_plan(plan)
    editor_url = _loopback_editor_url(editor_url)
    client = WorkEditorClient(editor_url, {})
    initial_status = client.get("/api/status")
    if initial_status.get("editingMode") == "authoring":
        return _run_authoring_api_edit(
            editor_url, plan, initial_status,
            screenshots_dir=screenshots_dir, browser_executable=browser_executable,
            harness=harness,
        )
    if harness is None:
        with BrowserHarness(browser_executable) as owned_harness:
            return run_browser_edit(
                editor_url,
                plan,
                screenshots_dir=screenshots_dir,
                harness=owned_harness,
            )

    with harness.page("bentoBrowserEdit") as page:
        page.goto(editor_url, wait_until="load")
        page.wait_for_function("window.bento && document.querySelectorAll('.ed-thumb').length > 0")
        page.wait_for_function(
            "document.querySelector('#work-save-validate') && "
            "!document.querySelector('#work-save-validate').disabled"
        )
        status_before = page.evaluate(
            "() => fetch('/api/status', {cache:'no-store'}).then(response => response.json())"
        )
        reported_editing_mode = status_before.get("editingMode")
        editing_mode = "final" if reported_editing_mode == "finalization" else reported_editing_mode
        _require(editing_mode in {"authoring", "final"}, f"Unsupported Work editor mode: {editing_mode!r}")
        expected_revision = plan.get("expectedDocumentRevision")
        if expected_revision is not None:
            _require(
                expected_revision == status_before.get("documentRevision"),
                "Browser edit plan is stale; reload the current Work editor revision",
            )
        before_document = extract_bento_doc(page.evaluate("window.bento.serialize()"))
        validate_bento_doc(before_document)
        before_hashes = {slide["id"]: _digest(slide) for slide in before_document["slides"]}
        requested_slide_ids: list[str] = []
        operation_results: list[dict[str, Any]] = []

        for index, operation in enumerate(plan["operations"]):
            name = operation["op"]
            slide_id = operation["slideId"]
            if slide_id not in requested_slide_ids:
                requested_slide_ids.append(slide_id)
            allowed_modes = CAPABILITIES[name]["modes"]
            _require(editing_mode in allowed_modes, f"{name!r} is not allowed in {editing_mode!r} mode")
            _, slide = _activate_slide(page, slide_id, harness)
            result: dict[str, Any] = {"index": index, "op": name, "slideId": slide_id}

            if name == "add-rectangle":
                before_ids = set(page.evaluate(
                    "slideId => window.bento.doc.slides.find(slide => slide.id === slideId).elements.map(element => element.id)",
                    slide_id,
                ))
                _named_button(page, ("図形", "Shape")).click()
                _named_button(page, ("長方形", "Rectangle")).click()
                _set_shape_properties(page, operation.get("properties", {}))
                order = operation["order"]
                if order == "back":
                    _order(page, "back")
                elif order == "background-card":
                    _order(page, "back")
                    _order(page, "forward")
                after_ids = set(page.evaluate(
                    "slideId => window.bento.doc.slides.find(slide => slide.id === slideId).elements.map(element => element.id)",
                    slide_id,
                ))
                added = sorted(after_ids - before_ids)
                _require(len(added) == 1, f"add-rectangle created unexpected elements: {added!r}")
                result["elementId"] = added[0]
            elif name == "align":
                element_ids = operation["elementIds"]
                current_slide = page.evaluate(
                    "slideId => JSON.parse(JSON.stringify(window.bento.doc.slides.find(slide => slide.id === slideId)))",
                    slide_id,
                )
                targets = [
                    next((element for element in current_slide["elements"] if element["id"] == element_id), None)
                    for element_id in element_ids
                ]
                _require(all(target is not None for target in targets), "align target is missing")
                _require(
                    all(target.get("type") in {"shape", "text"} for target in targets),
                    "align currently supports editable shape and text elements",
                )
                left = min(target["x"] for target in targets)
                right = max(target["x"] + target["w"] for target in targets)
                top = min(target["y"] for target in targets)
                bottom = max(target["y"] + target["h"] for target in targets)
                alignment = operation["alignment"]
                for target in targets:
                    _select_element(page, current_slide, target["id"], harness)
                    panel = _panel(
                        page, ("図形", "SHAPE") if target["type"] == "shape" else ("テキスト", "TEXT"),
                    )
                    if alignment == "left":
                        _set_number(panel, ("X",), left)
                    elif alignment == "center-x":
                        _set_number(panel, ("X",), (left + right - target["w"]) / 2)
                    elif alignment == "right":
                        _set_number(panel, ("X",), right - target["w"])
                    elif alignment == "top":
                        _set_number(panel, ("Y",), top)
                    elif alignment == "center-y":
                        _set_number(panel, ("Y",), (top + bottom - target["h"]) / 2)
                    else:
                        _set_number(panel, ("Y",), bottom - target["h"])
                result["elementIds"] = element_ids
            else:
                element = _select_element(page, slide, operation["elementId"], harness)
                result["elementId"] = operation["elementId"]
                if name == "set-shape":
                    _require(element.get("type") == "shape", "set-shape requires a shape element")
                    _set_shape_properties(page, operation.get("properties", {}))
                elif name == "set-text-style":
                    _require(element.get("type") == "text", "set-text-style requires a text element")
                    _set_text_style(page, operation.get("properties", {}))
                elif name == "set-text":
                    _require(element.get("type") == "text", "set-text requires a text element")
                    _require(
                        not element.get("equationId") and not element.get("latexSource")
                        and not str(element.get("html", "")).startswith("$$"),
                        "set-text refuses equations; use the registered equation authoring route",
                    )
                    target = page.locator(f'.ed-stage [data-el-id="{operation["elementId"]}"]')
                    target.dblclick(position={"x": 10, "y": 10})
                    editable = target.locator('.bento-text-inner[contenteditable="true"]')
                    _require(editable.count() == 1, "Bento inline text editor did not open")
                    editable.fill(operation["text"])
                    page.keyboard.press("Escape")
                elif name == "set-order":
                    _order(page, operation["order"])
                elif name == "duplicate":
                    before_ids = set(item["id"] for item in slide["elements"])
                    page.keyboard.press("Control+d")
                    after_ids = set(page.evaluate(
                        "slideId => window.bento.doc.slides.find(slide => slide.id === slideId).elements.map(element => element.id)",
                        slide_id,
                    ))
                    added = sorted(after_ids - before_ids)
                    _require(len(added) == 1, f"duplicate created unexpected elements: {added!r}")
                    result["newElementId"] = added[0]
                elif name == "delete":
                    page.keyboard.press("Delete")
                    remaining = page.evaluate(
                        "slideId => window.bento.doc.slides.find(slide => slide.id === slideId).elements.map(element => element.id)",
                        slide_id,
                    )
                    _require(operation["elementId"] not in remaining, "delete did not remove the target element")
            operation_results.append(result)

        proposed_html = page.evaluate("window.bento.serialize()")
        proposed_document = extract_bento_doc(proposed_html)
        validate_bento_doc(proposed_document)
        proposed_hashes = {slide["id"]: _digest(slide) for slide in proposed_document["slides"]}
        changed_before_save = [
            slide_id for slide_id in before_hashes
            if before_hashes[slide_id] != proposed_hashes.get(slide_id)
        ]
        unexpected = [slide_id for slide_id in changed_before_save if slide_id not in requested_slide_ids]
        if editing_mode != "authoring":
            _require(not unexpected, f"Browser edit changed unrequested slides before save: {unexpected!r}")
        # Fail closed before the mutation when the page attempted any unknown
        # remote request while the proposal was being prepared.  Known Bento
        # runtime probes remain blocked but do not block a local edit.
        harness.assert_no_blocked_network("bentoBrowserEdit")

        if not changed_before_save:
            _require(status_before.get("validation") == "pass", "Current Work editor document is not valid")
            return {
                "format": REPORT_FORMAT,
                "editorUrl": editor_url,
                "editingMode": editing_mode,
                "beforeDocumentRevision": status_before.get("documentRevision"),
                "afterDocumentRevision": status_before.get("documentRevision"),
                "registryRevisionUnchanged": True,
                "requestedSlideIds": requested_slide_ids,
                "changedSlideIds": [],
                "operations": operation_results,
                "saveStatus": "no-op",
                "saveMessage": "No document changes; save skipped",
                "validation": "pass",
                "validationScope": "changed-slides",
                "fullDeckBrowserCheck": "deferred",
                "screenshots": [],
            }

        save_result: dict[str, Any] = {}
        if editing_mode == "authoring":
            save_result = page.evaluate(
                """
                async payload => {
                  const response = await fetch('/api/save', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(payload)
                  });
                  const body = await response.json();
                  if (!response.ok) throw new Error(body.error || `Save failed (${response.status})`);
                  return body;
                }
                """,
                {
                    "baseDocumentRevision": status_before.get("documentRevision"),
                    "baseRegistryRevision": status_before.get("registryRevision"),
                    "serializedHtml": proposed_html,
                    "scopedSlideIds": requested_slide_ids,
                    "operation": "browser-edit",
                    "operationReport": {
                        "format": REPORT_FORMAT,
                        "requestedSlideIds": requested_slide_ids,
                        "preSaveUnrequestedSlideIds": unexpected,
                        "operations": operation_results,
                    },
                },
            )
            save_message = (
                "変更なし" if save_result.get("noOp")
                else f"保存済み: {save_result.get('documentRevision', '')}"
            )
        else:
            initial_message = page.locator("#bento-work-editor-status").inner_text()
            page.locator("#work-save-validate").click()
            page.wait_for_function(
                "before => { const text=document.querySelector('#bento-work-editor-status')?.textContent || ''; "
                "return text !== before && (text.includes('revision') || text.includes('検証')); }",
                arg=initial_message,
            )
            save_message = page.locator("#bento-work-editor-status").inner_text()
        status_after = page.evaluate(
            "() => fetch('/api/status', {cache:'no-store'}).then(response => response.json())"
        )
        _require(
            status_before.get("registryRevision") == status_after.get("registryRevision"),
            "Presentation-only browser edit unexpectedly changed the registry",
        )
        _require(status_after.get("validation") == "pass", "Work editor did not validate the saved document")
        if changed_before_save and not save_result.get("noOp"):
            _require(
                status_before.get("documentRevision") != status_after.get("documentRevision"),
                "Work editor did not advance the document revision after saving",
            )

        screenshots: list[str] = []
        saved_changed_slide_ids = save_result.get("savedChangedSlideIds", changed_before_save)
        discarded_slide_ids = save_result.get("discardedSlideIds", [])
        discarded_document_fields = save_result.get("discardedDocumentFields", [])
        submitted_changed_slide_ids = save_result.get("submittedChangedSlideIds", changed_before_save)
        changed_after_save = list(saved_changed_slide_ids)
        try:
            if editing_mode == "authoring":
                saved_html = page.evaluate(
                    "() => fetch('/?post-save-snapshot=1', {cache:'no-store'}).then(response => response.text())"
                )
                after_document = extract_bento_doc(saved_html)
                validate_bento_doc(after_document)
                after_hashes = {slide["id"]: _digest(slide) for slide in after_document["slides"]}
                _require(
                    all(after_hashes.get(slide_id) == proposed_hashes.get(slide_id)
                        for slide_id in saved_changed_slide_ids),
                    "Saved scoped browser edit differs from the reviewed slide proposal",
                )
            else:
                page.reload(wait_until="load")
                page.wait_for_function("window.bento && document.querySelectorAll('.ed-thumb').length > 0")
                after_document = extract_bento_doc(page.evaluate("window.bento.serialize()"))
                validate_bento_doc(after_document)
                after_hashes = {slide["id"]: _digest(slide) for slide in after_document["slides"]}
                changed_after_save = [
                    slide_id for slide_id in before_hashes
                    if before_hashes[slide_id] != after_hashes.get(slide_id)
                ]
                _require(
                    changed_after_save == saved_changed_slide_ids,
                    "Saved browser edit differs from the reviewed proposal",
                )

            if screenshots_dir is not None:
                output_dir = Path(screenshots_dir)
                slide_indexes = {slide["id"]: index for index, slide in enumerate(after_document["slides"])}
                for slide_id in changed_after_save:
                    output = output_dir / f"slide-{slide_indexes[slide_id] + 1}.png"
                    _capture_slide(page, slide_id, output, harness)
                    screenshots.append(str(output.resolve()))
            harness.assert_no_blocked_network("bentoBrowserEdit")
        except Exception as exc:
            report = {
                "format": REPORT_FORMAT,
                "editorUrl": editor_url,
                "editingMode": editing_mode,
                "beforeDocumentRevision": status_before.get("documentRevision"),
                "afterDocumentRevision": status_after.get("documentRevision"),
                "registryRevisionUnchanged": (
                    status_before.get("registryRevision") == status_after.get("registryRevision")
                ),
                "requestedSlideIds": requested_slide_ids,
                "changedSlideIds": changed_before_save,
                "submittedChangedSlideIds": submitted_changed_slide_ids,
                "discardedSlideIds": discarded_slide_ids,
                "preSaveUnrequestedSlideIds": unexpected,
                "discardedDocumentFields": discarded_document_fields,
                "operations": operation_results,
                "saveStatus": "committed",
                "saveMessage": save_message,
                "validation": "failed",
                "validationScope": "post-save",
                "postSaveError": str(exc),
                "fullDeckBrowserCheck": "deferred",
                "screenshots": screenshots,
            }
            raise BrowserEditPostSaveError(str(exc), report) from exc

    return {
        "format": REPORT_FORMAT,
        "editorUrl": editor_url,
        "editingMode": editing_mode,
        "beforeDocumentRevision": status_before.get("documentRevision"),
        "afterDocumentRevision": status_after.get("documentRevision"),
        "registryRevisionUnchanged": True,
        "requestedSlideIds": requested_slide_ids,
        "changedSlideIds": changed_after_save,
        "submittedChangedSlideIds": submitted_changed_slide_ids,
        "discardedSlideIds": discarded_slide_ids,
        "preSaveUnrequestedSlideIds": unexpected,
        "discardedDocumentFields": discarded_document_fields,
        "operations": operation_results,
        "saveStatus": "no-op" if save_result.get("noOp") else "committed",
        "saveMessage": save_message,
        "validation": "pass",
        "validationScope": "changed-slides",
        "fullDeckBrowserCheck": "deferred",
        "screenshots": screenshots,
    }
