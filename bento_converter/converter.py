"""Convert validated GPT design JSON into Bento Slides document JSON."""

from __future__ import annotations

import html
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .design_validator import validate_design
from .errors import ConversionError

DOC_ID_NAMESPACE = uuid.UUID("8d80fd7a-334f-5b55-9a10-08f06cbf662f")
SOURCE_ONLY_THEME_FIELDS = ("surface", "primary", "muted", "line")


@dataclass(frozen=True)
class ConversionResult:
    document: dict[str, Any]
    warnings: tuple[str, ...] = ()


def canonical_design_json(design: dict[str, Any]) -> str:
    return json.dumps(
        design,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def stable_doc_id(design: dict[str, Any]) -> str:
    return str(uuid.uuid5(DOC_ID_NAMESPACE, canonical_design_json(design)))


def _validate_uuid(value: str, source: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ConversionError(f"{source} must be a UUID string, got {value!r}") from exc


def resolve_doc_id(design: dict[str, Any], override: str | None = None) -> str:
    if override:
        return _validate_uuid(override, "--doc-id")
    configured = design["document"].get("docId")
    if configured:
        return _validate_uuid(configured, "document.docId")
    return stable_doc_id(design)


def _validate_timestamp(value: str, source: str) -> str:
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ConversionError(f"{source} must be an ISO-8601 timestamp, got {value!r}") from exc
    if parsed.tzinfo is None:
        raise ConversionError(f"{source} must include a timezone, got {value!r}")
    return value


def resolve_modified(design: dict[str, Any], override: str | None = None) -> str:
    if override == "now":
        return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    if override:
        return _validate_timestamp(override, "--modified")
    configured = design["document"].get("modified")
    if configured:
        return _validate_timestamp(configured, "document.modified")
    raise ConversionError(
        "A deterministic modified timestamp is required: set document.modified, "
        "pass --modified <ISO-8601>, or explicitly opt into --modified now."
    )


def text_content_to_html(content: str) -> str:
    """Treat GPT content as plain text and express newlines as native Bento breaks."""

    return html.escape(content, quote=False).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


def _common(element: dict[str, Any]) -> dict[str, Any]:
    converted: dict[str, Any] = {
        "id": element["id"],
        "x": element["x"],
        "y": element["y"],
        "w": element["w"],
        "h": element["h"],
        "rotation": 0,
        "opacity": 1,
    }
    if element.get("role") is not None:
        converted["role"] = element["role"]
    return converted


def _text_fields(style: dict[str, Any]) -> dict[str, Any]:
    converted = {
        "fontSize": style["fontSize"],
        "fontFamily": style.get("fontFamily", "sans-serif"),
        "fontWeight": style.get("fontWeight", 400),
        "color": style["color"],
        "align": style["align"],
        "valign": style["valign"],
        "lineHeight": style.get("lineHeight", 1.25),
    }
    if "letterSpacing" in style:
        converted["letterSpacing"] = style["letterSpacing"]
    return converted


def _convert_element(element: dict[str, Any]) -> dict[str, Any]:
    element_type = element["type"]
    if element_type == "text":
        return {
            **_common(element),
            "type": "text",
            "html": text_content_to_html(element["content"]),
            **_text_fields(element["style"]),
        }
    if element_type == "shape":
        style = element["style"]
        radius = style.get("cornerRadius", 0) if element["shape"] == "rounded-rectangle" else 0
        return {
            **_common(element),
            "type": "shape",
            "shape": "rect",
            "fill": style["fill"],
            "stroke": style["stroke"],
            "strokeWidth": style["strokeWidth"],
            "radius": radius,
        }
    if element_type == "latex":
        latex = element.get("latex")
        source = element.get("bentoSource") or f"$${latex}$$"
        raw_latex = latex if latex is not None else source[2:-2].strip()
        converted = {
            **_common(element),
            "type": "text",
            "html": source,
            **_text_fields(element["style"]),
        }
        if element.get("equationId") is not None:
            converted["equationId"] = element["equationId"]
        converted["latexSource"] = raw_latex
        return converted
    raise ConversionError(f"Unsupported element type reached conversion: {element_type!r}")


def convert_design(
    design: dict[str, Any],
    *,
    doc_id: str | None = None,
    modified: str | None = None,
) -> ConversionResult:
    validation = validate_design(design)
    source_document = design["document"]
    source_theme = source_document["theme"]
    warnings = list(validation.warnings)
    for field in SOURCE_ONLY_THEME_FIELDS:
        if field in source_theme:
            warnings.append(
                f"document.theme.{field} is a GPT source token with no Bento Theme field; "
                "it remains in the design JSON and is omitted from the native theme."
            )

    slides: list[dict[str, Any]] = []
    for source_slide in design["slides"]:
        indexed = list(enumerate(source_slide["elements"]))
        indexed.sort(key=lambda pair: (pair[1].get("z", 0), pair[0]))
        slides.append(
            {
                "id": source_slide["id"],
                "background": source_slide["background"],
                "transition": "none",
                "notes": "",
                "elements": [_convert_element(element) for _, element in indexed],
            }
        )

    document: dict[str, Any] = {
        "format": "bento/slides",
        "version": 1,
        "docId": resolve_doc_id(design, doc_id),
        "title": source_document["title"],
        "size": {
            "width": source_document["canvas"]["width"],
            "height": source_document["canvas"]["height"],
        },
        "theme": {
            "background": source_theme["background"],
            "color": source_theme["text"],
            "accent": source_theme["accent"],
            "fontFamily": source_theme.get("fontFamily", "sans-serif"),
        },
        "slides": slides,
        "modified": resolve_modified(design, modified),
    }
    return ConversionResult(document=document, warnings=tuple(warnings))

