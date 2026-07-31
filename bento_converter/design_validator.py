"""Validate GPT design JSON before any Bento conversion occurs."""

from __future__ import annotations

import math
from typing import Any

from .errors import DesignValidationError, ValidationReport, issue

ALLOWED_TYPES = {"text", "shape", "latex"}
ALLOWED_ALIGN = {"left", "center", "right"}
ALLOWED_VALIGN = {"top", "middle", "bottom"}
SUPPORTED_SHAPES = {"rounded-rectangle", "rectangle", "rect"}

COMMON_ELEMENT_FIELDS = {"id", "role", "type", "x", "y", "w", "h", "z", "style"}
TYPE_FIELDS = {
    "text": {"content"},
    "shape": {"shape"},
    "latex": {"latex", "bentoSource", "equationId"},
}
TEXT_STYLE_FIELDS = {
    "fontSize",
    "fontWeight",
    "fontFamily",
    "color",
    "align",
    "valign",
    "lineHeight",
    "letterSpacing",
}
SHAPE_STYLE_FIELDS = {"fill", "stroke", "strokeWidth", "cornerRadius"}
DOCUMENT_FIELDS = {"docId", "modified", "title", "canvas", "theme"}
THEME_FIELDS = {
    "background",
    "surface",
    "primary",
    "accent",
    "text",
    "muted",
    "line",
    "fontFamily",
}
SLIDE_FIELDS = {"id", "background", "elements"}


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_text_style(
    style: object,
    *,
    slide_id: object,
    element_id: object,
    errors: list[str],
    warnings: list[str],
) -> None:
    if not isinstance(style, dict):
        errors.append(
            issue(
                slide_id=slide_id,
                element_id=element_id,
                field="style",
                actual=style,
                fix="Provide a style object with fontSize, color, align, and valign.",
            )
        )
        return
    for field in sorted(set(style) - TEXT_STYLE_FIELDS):
        warnings.append(
            issue(
                slide_id=slide_id,
                element_id=element_id,
                field=f"style.{field}",
                actual=style[field],
                fix="Remove the unsupported style or add an explicit converter mapping.",
            )
        )
    if not _is_number(style.get("fontSize")) or style.get("fontSize", 0) <= 0:
        errors.append(
            issue(
                slide_id=slide_id,
                element_id=element_id,
                field="style.fontSize",
                actual=style.get("fontSize"),
                fix="Use a positive numeric fontSize.",
            )
        )
    for field in ("color", "align", "valign"):
        if not _nonempty_string(style.get(field)):
            errors.append(
                issue(
                    slide_id=slide_id,
                    element_id=element_id,
                    field=f"style.{field}",
                    actual=style.get(field),
                    fix=f"Provide a non-empty {field} string.",
                )
            )
    if isinstance(style.get("align"), str) and style["align"] not in ALLOWED_ALIGN:
        errors.append(
            issue(
                slide_id=slide_id,
                element_id=element_id,
                field="style.align",
                actual=style["align"],
                fix=f"Use one of {sorted(ALLOWED_ALIGN)}.",
            )
        )
    if isinstance(style.get("valign"), str) and style["valign"] not in ALLOWED_VALIGN:
        errors.append(
            issue(
                slide_id=slide_id,
                element_id=element_id,
                field="style.valign",
                actual=style["valign"],
                fix=f"Use one of {sorted(ALLOWED_VALIGN)}.",
            )
        )
    for field in ("fontWeight", "lineHeight"):
        if field in style and (not _is_number(style[field]) or style[field] <= 0):
            errors.append(
                issue(
                    slide_id=slide_id,
                    element_id=element_id,
                    field=f"style.{field}",
                    actual=style[field],
                    fix=f"Use a positive numeric {field}.",
                )
            )


def _validate_shape_style(
    style: object,
    *,
    slide_id: object,
    element_id: object,
    errors: list[str],
    warnings: list[str],
) -> None:
    if not isinstance(style, dict):
        errors.append(
            issue(
                slide_id=slide_id,
                element_id=element_id,
                field="style",
                actual=style,
                fix="Provide a shape style object.",
            )
        )
        return
    for field in sorted(set(style) - SHAPE_STYLE_FIELDS):
        warnings.append(
            issue(
                slide_id=slide_id,
                element_id=element_id,
                field=f"style.{field}",
                actual=style[field],
                fix="Remove the unsupported style or add an explicit converter mapping.",
            )
        )
    for field in ("fill", "stroke"):
        if not _nonempty_string(style.get(field)):
            errors.append(
                issue(
                    slide_id=slide_id,
                    element_id=element_id,
                    field=f"style.{field}",
                    actual=style.get(field),
                    fix=f"Provide a non-empty CSS color string for {field}.",
                )
            )
    if not _is_number(style.get("strokeWidth")) or style.get("strokeWidth", -1) < 0:
        errors.append(
            issue(
                slide_id=slide_id,
                element_id=element_id,
                field="style.strokeWidth",
                actual=style.get("strokeWidth"),
                fix="Use a non-negative numeric strokeWidth.",
            )
        )
    if "cornerRadius" in style and (
        not _is_number(style["cornerRadius"]) or style["cornerRadius"] < 0
    ):
        errors.append(
            issue(
                slide_id=slide_id,
                element_id=element_id,
                field="style.cornerRadius",
                actual=style["cornerRadius"],
                fix="Use a non-negative numeric cornerRadius.",
            )
        )


def validate_design(design: dict[str, Any]) -> ValidationReport:
    errors: list[str] = []
    warnings: list[str] = []

    if not _nonempty_string(design.get("format")):
        errors.append(
            issue(
                field="format",
                actual=design.get("format"),
                fix="Set format to a non-empty GPT design format identifier.",
            )
        )
    document = design.get("document")
    if not isinstance(document, dict):
        raise DesignValidationError(
            [
                issue(
                    field="document",
                    actual=document,
                    fix="Provide a document object containing title, canvas, and theme.",
                )
            ]
        )
    for field in sorted(set(document) - DOCUMENT_FIELDS):
        warnings.append(
            issue(
                field=f"document.{field}",
                actual=document[field],
                fix="Remove the unknown document field or add an explicit mapping.",
            )
        )
    if not isinstance(document.get("title"), str):
        errors.append(
            issue(
                field="document.title",
                actual=document.get("title"),
                fix="Provide a title string.",
            )
        )
    if "docId" in document and not _nonempty_string(document["docId"]):
        errors.append(
            issue(
                field="document.docId",
                actual=document.get("docId"),
                fix="Provide a UUID string or omit docId for stable UUID generation.",
            )
        )
    if "modified" in document and not _nonempty_string(document["modified"]):
        errors.append(
            issue(
                field="document.modified",
                actual=document.get("modified"),
                fix="Provide an ISO-8601 timestamp or omit it and pass --modified.",
            )
        )
    canvas = document.get("canvas")
    if not isinstance(canvas, dict):
        errors.append(
            issue(
                field="document.canvas",
                actual=canvas,
                fix="Provide canvas.width and canvas.height.",
            )
        )
        width = height = None
    else:
        width, height = canvas.get("width"), canvas.get("height")
        for field, value in (("width", width), ("height", height)):
            if not _is_number(value) or value <= 0:
                errors.append(
                    issue(
                        field=f"document.canvas.{field}",
                        actual=value,
                        fix=f"Use a positive numeric canvas {field}.",
                    )
                )
    theme = document.get("theme")
    if not isinstance(theme, dict):
        errors.append(
            issue(
                field="document.theme",
                actual=theme,
                fix="Provide a theme object.",
            )
        )
    else:
        for field in sorted(set(theme) - THEME_FIELDS):
            warnings.append(
                issue(
                    field=f"document.theme.{field}",
                    actual=theme[field],
                    fix="Remove the unknown theme field or add an explicit mapping.",
                )
            )
        for field in ("background", "accent", "text"):
            if not _nonempty_string(theme.get(field)):
                errors.append(
                    issue(
                        field=f"document.theme.{field}",
                        actual=theme.get(field),
                        fix=f"Provide a non-empty CSS color string for {field}.",
                    )
                )

    slides = design.get("slides")
    if not isinstance(slides, list) or not slides:
        errors.append(
            issue(
                field="slides",
                actual=slides,
                fix="Provide a non-empty slides array.",
            )
        )
        slides = []

    slide_ids: set[str] = set()
    for slide_index, slide in enumerate(slides):
        if not isinstance(slide, dict):
            errors.append(
                issue(
                    slide_id=f"<index:{slide_index}>",
                    field="slide",
                    actual=slide,
                    fix="Use an object for every slide.",
                )
            )
            continue
        slide_id = slide.get("id", f"<index:{slide_index}>")
        if not _nonempty_string(slide.get("id")):
            errors.append(
                issue(
                    slide_id=slide_id,
                    field="slide.id",
                    actual=slide.get("id"),
                    fix="Provide a non-empty slide id.",
                )
            )
        elif slide["id"] in slide_ids:
            errors.append(
                issue(
                    slide_id=slide["id"],
                    field="slide.id",
                    actual=slide["id"],
                    fix="Use a document-wide unique slide id.",
                )
            )
        else:
            slide_ids.add(slide["id"])
        for field in sorted(set(slide) - SLIDE_FIELDS):
            warnings.append(
                issue(
                    slide_id=slide_id,
                    field=f"slide.{field}",
                    actual=slide[field],
                    fix="Remove the unknown slide field or add an explicit mapping.",
                )
            )
        if not _nonempty_string(slide.get("background")):
            errors.append(
                issue(
                    slide_id=slide_id,
                    field="slide.background",
                    actual=slide.get("background"),
                    fix="Provide a non-empty CSS background string.",
                )
            )
        elements = slide.get("elements")
        if not isinstance(elements, list):
            errors.append(
                issue(
                    slide_id=slide_id,
                    field="slide.elements",
                    actual=elements,
                    fix="Provide an elements array.",
                )
            )
            continue
        element_ids: set[str] = set()
        for element_index, element in enumerate(elements):
            if not isinstance(element, dict):
                errors.append(
                    issue(
                        slide_id=slide_id,
                        element_id=f"<index:{element_index}>",
                        field="element",
                        actual=element,
                        fix="Use an object for every element.",
                    )
                )
                continue
            element_id = element.get("id", f"<index:{element_index}>")
            if not _nonempty_string(element.get("id")):
                errors.append(
                    issue(
                        slide_id=slide_id,
                        element_id=element_id,
                        field="element.id",
                        actual=element.get("id"),
                        fix="Provide a non-empty element id.",
                    )
                )
            elif element["id"] in element_ids:
                errors.append(
                    issue(
                        slide_id=slide_id,
                        element_id=element["id"],
                        field="element.id",
                        actual=element["id"],
                        fix="Use an id that is unique within this slide.",
                    )
                )
            else:
                element_ids.add(element["id"])
            element_type = element.get("type")
            if element_type not in ALLOWED_TYPES:
                errors.append(
                    issue(
                        slide_id=slide_id,
                        element_id=element_id,
                        field="element.type",
                        actual=element_type,
                        fix=f"Use one of {sorted(ALLOWED_TYPES)}.",
                    )
                )
            known_fields = COMMON_ELEMENT_FIELDS | TYPE_FIELDS.get(element_type, set())
            for field in sorted(set(element) - known_fields):
                warnings.append(
                    issue(
                        slide_id=slide_id,
                        element_id=element_id,
                        field=f"element.{field}",
                        actual=element[field],
                        fix="Remove the unknown field or add an explicit converter mapping.",
                    )
                )
            frame: dict[str, float] = {}
            for field in ("x", "y", "w", "h"):
                value = element.get(field)
                if not _is_number(value):
                    errors.append(
                        issue(
                            slide_id=slide_id,
                            element_id=element_id,
                            field=field,
                            actual=value,
                            fix=f"Use a finite numeric {field} coordinate.",
                        )
                    )
                else:
                    frame[field] = value
            for field in ("w", "h"):
                if field in frame and frame[field] <= 0:
                    errors.append(
                        issue(
                            slide_id=slide_id,
                            element_id=element_id,
                            field=field,
                            actual=frame[field],
                            fix=f"Use a positive {field}.",
                        )
                    )
            if "z" in element and not _is_number(element["z"]):
                errors.append(
                    issue(
                        slide_id=slide_id,
                        element_id=element_id,
                        field="z",
                        actual=element["z"],
                        fix="Use a finite numeric z value or omit z for 0.",
                    )
                )
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
                errors.append(
                    issue(
                        slide_id=slide_id,
                        element_id=element_id,
                        field="x/y/w/h",
                        actual=frame,
                        fix=f"Keep the complete frame inside the {width}x{height} canvas.",
                    )
                )

            if element_type == "text":
                if not isinstance(element.get("content"), str):
                    errors.append(
                        issue(
                            slide_id=slide_id,
                            element_id=element_id,
                            field="content",
                            actual=element.get("content"),
                            fix="Provide text content as a string.",
                        )
                    )
                _validate_text_style(
                    element.get("style"),
                    slide_id=slide_id,
                    element_id=element_id,
                    errors=errors,
                    warnings=warnings,
                )
            elif element_type == "shape":
                if element.get("shape") not in SUPPORTED_SHAPES:
                    errors.append(
                        issue(
                            slide_id=slide_id,
                            element_id=element_id,
                            field="shape",
                            actual=element.get("shape"),
                            fix=f"Use one of {sorted(SUPPORTED_SHAPES)}.",
                        )
                    )
                _validate_shape_style(
                    element.get("style"),
                    slide_id=slide_id,
                    element_id=element_id,
                    errors=errors,
                    warnings=warnings,
                )
            elif element_type == "latex":
                latex = element.get("latex")
                source = element.get("bentoSource")
                if not _nonempty_string(latex) and not _nonempty_string(source):
                    errors.append(
                        issue(
                            slide_id=slide_id,
                            element_id=element_id,
                            field="latex/bentoSource",
                            actual={"latex": latex, "bentoSource": source},
                            fix="Provide latex or a $$...$$ bentoSource.",
                        )
                    )
                if source is not None:
                    if not _nonempty_string(source) or not source.startswith("$$") or not source.endswith("$$"):
                        errors.append(
                            issue(
                                slide_id=slide_id,
                                element_id=element_id,
                                field="bentoSource",
                                actual=source,
                                fix="Wrap the complete display-math source in $$ delimiters.",
                            )
                        )
                    elif _nonempty_string(latex) and source[2:-2].strip() != latex.strip():
                        errors.append(
                            issue(
                                slide_id=slide_id,
                                element_id=element_id,
                                field="bentoSource",
                                actual=source,
                                fix="Make the content inside $$...$$ equal to latex.",
                            )
                        )
                if "equationId" in element and not _nonempty_string(element["equationId"]):
                    errors.append(
                        issue(
                            slide_id=slide_id,
                            element_id=element_id,
                            field="equationId",
                            actual=element["equationId"],
                            fix="Use a non-empty equationId or omit it.",
                        )
                    )
                _validate_text_style(
                    element.get("style"),
                    slide_id=slide_id,
                    element_id=element_id,
                    errors=errors,
                    warnings=warnings,
                )

    if errors:
        raise DesignValidationError(errors)
    return ValidationReport(tuple(warnings))

