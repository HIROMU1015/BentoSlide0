"""Validate converted Bento JSON and runtime-preserving HTML output."""

from __future__ import annotations

import html as html_lib
import math
import re
from typing import Any

from .errors import BentoValidationError, ValidationReport, issue
from .html_document import assert_runtime_integrity, extract_bento_doc, locate_bento_doc

ROOT_FIELDS = {
    "format",
    "version",
    "docId",
    "title",
    "size",
    "theme",
    "slides",
    "modified",
    "present",
    "assets",
    "fonts",
    "layouts",
    "collab",
    "template",
    "readonly",
    "meta",
}
SLIDE_FIELDS = {
    "id",
    "background",
    "transition",
    "elements",
    "notes",
    "name",
    "stateOf",
    "hover",
    "comments",
}
ELEMENT_BASE_FIELDS = {
    "id",
    "morphId",
    "x",
    "y",
    "w",
    "h",
    "rotation",
    "opacity",
    "shadow",
    "blur",
    "blend",
    "backdropFilter",
    "fx",
    "link",
    "group",
    "groupId",
    "showOnHover",
    "role",
    "type",
}
TEXT_FIELDS = {
    "html",
    "fontSize",
    "fontFamily",
    "fontWeight",
    "color",
    "colorGradient",
    "align",
    "valign",
    "lineHeight",
    "letterSpacing",
    "textStroke",
    "placeholder",
}
SHAPE_FIELDS = {
    "shape",
    "fill",
    "fillGradient",
    "stroke",
    "strokeWidth",
    "radius",
    "strokeDash",
    "strokeStyle",
    "lineStart",
    "lineEnd",
    "d",
    "pathBox",
    "from",
    "to",
}
IMAGE_FIELDS = {"src", "fit", "radius"}
SVG_FIELDS = {"asset", "markup"}
MEDIA_FIELDS = {"kind", "src", "poster", "fit", "radius", "controls", "autoplay", "loop", "muted"}
CHART_FIELDS = {"preset", "option"}
TABLE_FIELDS = {"columns", "rows", "header", "style"}
CONVERTER_EXTENSION_FIELDS = {
    "equationId", "latexSource", "assetId", "figureId", "chartId", "tableId",
}
SUPPORTED_TYPES = {"text", "shape", "image", "svg", "media", "chart", "table"}
SHAPE_NAMES = {"rect", "ellipse", "triangle", "arrow", "line", "path"}


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_bento_doc(document: dict[str, Any]) -> ValidationReport:
    errors: list[str] = []
    warnings: list[str] = []
    if document.get("format") != "bento/slides":
        errors.append(
            issue(field="format", actual=document.get("format"), fix="Set format to 'bento/slides'.")
        )
    if document.get("version") != 1:
        errors.append(issue(field="version", actual=document.get("version"), fix="Use Bento format version 1."))
    for field in ("docId", "title", "modified"):
        if not _nonempty(document.get(field)):
            errors.append(
                issue(field=field, actual=document.get(field), fix=f"Provide a non-empty {field} string.")
            )
    size = document.get("size")
    if not isinstance(size, dict):
        errors.append(issue(field="size", actual=size, fix="Provide size.width and size.height."))
        width = height = None
    else:
        width, height = size.get("width"), size.get("height")
        for field, value in (("width", width), ("height", height)):
            if not _is_number(value) or value <= 0:
                errors.append(
                    issue(field=f"size.{field}", actual=value, fix=f"Use a positive numeric {field}.")
                )
    if not isinstance(document.get("theme"), dict):
        errors.append(issue(field="theme", actual=document.get("theme"), fix="Provide a Bento theme object."))
    if "assets" in document and (
        not isinstance(document["assets"], dict)
        or not all(_nonempty(key) and _nonempty(value) for key, value in document["assets"].items())
    ):
        errors.append(issue(field="assets", actual=document.get("assets"), fix="Use an object mapping stable asset ids to non-empty data URIs."))
    if "fonts" in document and not isinstance(document["fonts"], list):
        errors.append(issue(field="fonts", actual=document.get("fonts"), fix="Use an array of Bento font descriptors."))
    for field in sorted(set(document) - ROOT_FIELDS):
        warnings.append(issue(field=field, actual=document[field], fix="Unknown root field is preserved but not validated."))
    slides = document.get("slides")
    if not isinstance(slides, list) or not slides:
        errors.append(issue(field="slides", actual=slides, fix="Provide a non-empty slides array."))
        slides = []
    slide_ids: set[str] = set()
    for slide_index, slide in enumerate(slides):
        if not isinstance(slide, dict):
            errors.append(
                issue(slide_id=f"<index:{slide_index}>", field="slide", actual=slide, fix="Use a slide object.")
            )
            continue
        slide_id = slide.get("id", f"<index:{slide_index}>")
        if not _nonempty(slide.get("id")):
            errors.append(issue(slide_id=slide_id, field="slide.id", actual=slide.get("id"), fix="Provide a slide id."))
        elif slide["id"] in slide_ids:
            errors.append(issue(slide_id=slide_id, field="slide.id", actual=slide["id"], fix="Use a unique slide id."))
        else:
            slide_ids.add(slide["id"])
        for field in ("background", "transition", "notes"):
            if not isinstance(slide.get(field), str):
                errors.append(
                    issue(slide_id=slide_id, field=f"slide.{field}", actual=slide.get(field), fix=f"Provide a {field} string.")
                )
        for field in sorted(set(slide) - SLIDE_FIELDS):
            warnings.append(
                issue(slide_id=slide_id, field=f"slide.{field}", actual=slide[field], fix="Unknown slide field is preserved but not validated.")
            )
        elements = slide.get("elements")
        if not isinstance(elements, list):
            errors.append(
                issue(slide_id=slide_id, field="slide.elements", actual=elements, fix="Provide an elements array.")
            )
            continue
        ids: set[str] = set()
        for element_index, element in enumerate(elements):
            if not isinstance(element, dict):
                errors.append(
                    issue(slide_id=slide_id, element_id=f"<index:{element_index}>", field="element", actual=element, fix="Use an element object.")
                )
                continue
            element_id = element.get("id", f"<index:{element_index}>")
            if not _nonempty(element.get("id")):
                errors.append(issue(slide_id=slide_id, element_id=element_id, field="element.id", actual=element.get("id"), fix="Provide an element id."))
            elif element["id"] in ids:
                errors.append(issue(slide_id=slide_id, element_id=element_id, field="element.id", actual=element["id"], fix="Use an id unique within this slide."))
            else:
                ids.add(element["id"])
            frame: dict[str, float] = {}
            for field in ("x", "y", "w", "h"):
                value = element.get(field)
                if not _is_number(value):
                    errors.append(issue(slide_id=slide_id, element_id=element_id, field=field, actual=value, fix=f"Use a finite numeric {field}."))
                else:
                    frame[field] = value
            for field in ("w", "h"):
                if field in frame and frame[field] <= 0:
                    errors.append(issue(slide_id=slide_id, element_id=element_id, field=field, actual=frame[field], fix=f"Use a positive {field}."))
            if (
                width is not None
                and height is not None
                and all(field in frame for field in ("x", "y", "w", "h"))
                and (
                    frame["x"] < 0
                    or frame["y"] < 0
                    or frame["x"] + frame["w"] > width
                    or frame["y"] + frame["h"] > height
                )
            ):
                errors.append(issue(slide_id=slide_id, element_id=element_id, field="x/y/w/h", actual=frame, fix=f"Keep the complete frame inside the {width}x{height} canvas."))
            element_type = element.get("type")
            if element_type not in SUPPORTED_TYPES:
                warnings.append(issue(slide_id=slide_id, element_id=element_id, field="type", actual=element_type, fix="This native Bento type is not deeply validated by the current converter."))
                continue
            if element_type == "text":
                if not isinstance(element.get("html"), str):
                    errors.append(issue(slide_id=slide_id, element_id=element_id, field="html", actual=element.get("html"), fix="Provide text html as a string."))
                for field in ("fontSize", "lineHeight"):
                    if not _is_number(element.get(field)) or element.get(field, 0) <= 0:
                        errors.append(issue(slide_id=slide_id, element_id=element_id, field=field, actual=element.get(field), fix=f"Use a positive numeric {field}."))
                if "equationId" in element or "latexSource" in element:
                    source = element.get("html")
                    if not isinstance(source, str) or not source.startswith("$$") or not source.endswith("$$"):
                        errors.append(issue(slide_id=slide_id, element_id=element_id, field="html", actual=source, fix="Store equation source as a $$...$$ text html string."))
                    if not _nonempty(element.get("latexSource")):
                        errors.append(issue(slide_id=slide_id, element_id=element_id, field="latexSource", actual=element.get("latexSource"), fix="Provide the original LaTeX source for generated equations."))
            elif element_type == "shape":
                if element.get("shape") not in SHAPE_NAMES:
                    errors.append(issue(slide_id=slide_id, element_id=element_id, field="shape", actual=element.get("shape"), fix=f"Use one of {sorted(SHAPE_NAMES)}."))
            elif element_type == "image":
                if not _nonempty(element.get("src")):
                    errors.append(issue(slide_id=slide_id, element_id=element_id, field="src", actual=element.get("src"), fix="Provide an asset id, URL, or data URI."))
                if element.get("fit") not in {"cover", "contain", "fill"}:
                    errors.append(issue(slide_id=slide_id, element_id=element_id, field="fit", actual=element.get("fit"), fix="Use cover, contain, or fill."))
            elif element_type == "svg":
                if not _nonempty(element.get("asset")) and not _nonempty(element.get("markup")):
                    errors.append(issue(slide_id=slide_id, element_id=element_id, field="asset/markup", actual=None, fix="Provide SVG markup or an asset id."))
            elif element_type == "media":
                if element.get("kind") not in {"video", "audio"} or not _nonempty(element.get("src")):
                    errors.append(issue(slide_id=slide_id, element_id=element_id, field="kind/src", actual=(element.get("kind"), element.get("src")), fix="Use video/audio and a non-empty source."))
            elif element_type == "chart":
                if element.get("preset") not in {"bar", "line", "pie", "scatter"} or not isinstance(element.get("option"), dict):
                    errors.append(issue(slide_id=slide_id, element_id=element_id, field="preset/option", actual=(element.get("preset"), element.get("option")), fix="Use a supported preset and pure JSON option object."))
            elif element_type == "table":
                if not isinstance(element.get("columns"), list) or not isinstance(element.get("rows"), list):
                    errors.append(issue(slide_id=slide_id, element_id=element_id, field="columns/rows", actual=(element.get("columns"), element.get("rows")), fix="Use structured column and row arrays."))
                else:
                    columns = element["columns"]
                    for column_index, column in enumerate(columns):
                        if not isinstance(column, dict) or not _is_number(column.get("w")) or column["w"] <= 0:
                            errors.append(issue(slide_id=slide_id, element_id=element_id, field=f"columns[{column_index}].w", actual=column, fix="Use a positive fractional width."))
                    for row_index, row in enumerate(element["rows"]):
                        cells = row.get("cells") if isinstance(row, dict) else None
                        if not isinstance(cells, list) or len(cells) != len(columns):
                            errors.append(issue(slide_id=slide_id, element_id=element_id, field=f"rows[{row_index}].cells", actual=cells, fix="Use one structured cell per column."))
                            continue
                        for cell_index, cell in enumerate(cells):
                            if not isinstance(cell, dict) or not isinstance(cell.get("html"), str):
                                errors.append(issue(slide_id=slide_id, element_id=element_id, field=f"rows[{row_index}].cells[{cell_index}].html", actual=cell, fix="Provide inline HTML as a string."))
            type_fields = {
                "text": TEXT_FIELDS,
                "shape": SHAPE_FIELDS,
                "image": IMAGE_FIELDS,
                "svg": SVG_FIELDS,
                "media": MEDIA_FIELDS,
                "chart": CHART_FIELDS,
                "table": TABLE_FIELDS,
            }
            known = ELEMENT_BASE_FIELDS | type_fields.get(element_type, set())
            unknown = set(element) - known - CONVERTER_EXTENSION_FIELDS
            for field in sorted(unknown):
                warnings.append(issue(slide_id=slide_id, element_id=element_id, field=field, actual=element[field], fix="Unknown element field is preserved but not validated."))
    slide_by_id = {slide.get("id"): slide for slide in slides if isinstance(slide, dict) and _nonempty(slide.get("id"))}
    for slide_index, slide in enumerate(slides):
        if not isinstance(slide, dict):
            continue
        slide_id = slide.get("id")
        state_of = slide.get("stateOf")
        if state_of and state_of not in slide_by_id:
            errors.append(issue(slide_id=slide_id, field="stateOf", actual=state_of, fix="Reference an existing slide id."))
        element_ids = {element.get("id") for element in slide.get("elements", []) if isinstance(element, dict)}
        for element in slide.get("elements", []):
            if not isinstance(element, dict):
                continue
            link = element.get("link")
            if isinstance(link, str) and link and not re.match(r"^(?:https?://|mailto:|#)", link) and link not in slide_by_id:
                errors.append(issue(slide_id=slide_id, element_id=element.get("id"), field="link", actual=link, fix="Reference an existing slide/state id or an explicit external URL."))
            for endpoint_name in ("from", "to"):
                endpoint = element.get(endpoint_name)
                if endpoint is not None and (
                    not isinstance(endpoint, dict)
                    or endpoint.get("el") not in element_ids
                    or not _nonempty(endpoint.get("side"))
                ):
                    errors.append(issue(slide_id=slide_id, element_id=element.get("id"), field=endpoint_name, actual=endpoint, fix="Reference an element and side on the same slide."))
        if slide.get("transition") == "morph":
            previous = slides[slide_index - 1] if slide_index else None
            previous_keys = {
                element.get("morphId") or element.get("id")
                for element in previous.get("elements", [])
                if isinstance(element, dict)
            } if isinstance(previous, dict) else set()
            current_keys = {
                element.get("morphId") or element.get("id")
                for element in slide.get("elements", [])
                if isinstance(element, dict)
            }
            if not previous_keys.intersection(current_keys):
                errors.append(issue(slide_id=slide_id, field="transition", actual="morph", fix="Share at least one element id or morphId with the previous slide."))
    if errors:
        raise BentoValidationError(errors)
    return ValidationReport(tuple(warnings))


def _plain_text_html(content: str) -> str:
    return html_lib.escape(content, quote=False).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


def validate_conversion(design: dict[str, Any], document: dict[str, Any]) -> ValidationReport:
    """Cross-check output against source fields that cannot be inferred from Bento alone."""

    errors: list[str] = []
    native_slides = {slide.get("id"): slide for slide in document.get("slides", []) if isinstance(slide, dict)}
    for source_slide in design.get("slides", []):
        slide_id = source_slide.get("id")
        native_slide = native_slides.get(slide_id)
        if native_slide is None:
            errors.append(issue(slide_id=slide_id, field="slides", actual=None, fix="Emit every source slide."))
            continue
        expected_order = [
            element["id"]
            for _, element in sorted(
                enumerate(source_slide["elements"]),
                key=lambda pair: (pair[1].get("z", 0), pair[0]),
            )
        ]
        actual_order = [element.get("id") for element in native_slide.get("elements", [])]
        if actual_order != expected_order:
            errors.append(issue(slide_id=slide_id, field="elements[z-order]", actual=actual_order, fix=f"Use stable z-order {expected_order}."))
        native_elements = {element.get("id"): element for element in native_slide.get("elements", [])}
        for source in source_slide["elements"]:
            element_id = source["id"]
            native = native_elements.get(element_id)
            if native is None:
                errors.append(issue(slide_id=slide_id, element_id=element_id, field="element", actual=None, fix="Emit this source element."))
                continue
            for field in ("x", "y", "w", "h"):
                if native.get(field) != source.get(field):
                    errors.append(issue(slide_id=slide_id, element_id=element_id, field=field, actual=native.get(field), fix=f"Preserve source value {source.get(field)!r}."))
            if source["type"] == "text" and native.get("html") != _plain_text_html(source["content"]):
                errors.append(issue(slide_id=slide_id, element_id=element_id, field="html", actual=native.get("html"), fix="Escape plain text and convert newlines to <br>."))
            elif source["type"] == "shape" and (native.get("type"), native.get("shape")) != ("shape", "rect"):
                errors.append(issue(slide_id=slide_id, element_id=element_id, field="type/shape", actual=(native.get("type"), native.get("shape")), fix="Map rectangle shapes to native shape/rect."))
            elif source["type"] == "latex":
                expected = source.get("bentoSource") or f"$${source.get('latex')}$$"
                if native.get("type") != "text" or native.get("html") != expected:
                    errors.append(issue(slide_id=slide_id, element_id=element_id, field="type/html", actual=(native.get("type"), native.get("html")), fix=f"Store the editable source as text html {expected!r}."))
    if errors:
        raise BentoValidationError(errors)
    return ValidationReport()


def validate_bento_html(html: str, *, base_html: str | None = None) -> tuple[dict[str, Any], ValidationReport]:
    span = locate_bento_doc(html)
    raw = html[span.content_start : span.content_end]
    document = extract_bento_doc(html)
    report = validate_bento_doc(document)
    if "<" in raw:
        raise BentoValidationError(
            [issue(field="#bento-doc JSON", actual="contains literal '<'", fix="Escape every '<' as \\u003c before embedding.")]
        )
    if base_html is not None:
        assert_runtime_integrity(base_html, html)
    return document, report


def unknown_fields(document: dict[str, Any]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    root_unknown = sorted(set(document) - ROOT_FIELDS)
    if root_unknown:
        result["document"] = root_unknown
    for slide in document.get("slides", []):
        slide_id = slide.get("id", "<unknown>")
        slide_unknown = sorted(set(slide) - SLIDE_FIELDS)
        if slide_unknown:
            result[f"slide:{slide_id}"] = slide_unknown
        for element in slide.get("elements", []):
            element_type = element.get("type")
            type_fields = {"text": TEXT_FIELDS, "shape": SHAPE_FIELDS, "image": IMAGE_FIELDS, "svg": SVG_FIELDS, "media": MEDIA_FIELDS, "chart": CHART_FIELDS, "table": TABLE_FIELDS}
            known = ELEMENT_BASE_FIELDS | type_fields.get(element_type, set())
            custom = sorted(set(element) - known - CONVERTER_EXTENSION_FIELDS)
            if custom:
                result[f"element:{slide_id}/{element.get('id', '<unknown>')}"] = custom
    return result
