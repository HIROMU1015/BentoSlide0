"""Convert computed HTML layout plus registry metadata to native Bento JSON."""

from __future__ import annotations

import base64
import html as html_lib
import json
import math
import mimetypes
import re
import uuid
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .errors import ConversionError, ValidationError, issue
from .html_layout import CANVAS_HEIGHT, CANVAS_WIDTH, LayoutResult
from .html_source import SourceChapter
from .native_compatibility import classify_native_compatibility, classify_slide_background
from .resource_embedding import ResourceContext, embed_markup_resources, replace_css_urls, resolve_embedded_resource, scan_document_resources

DOC_ID_NAMESPACE = uuid.UUID("e03e3515-25d7-5dc9-891c-a2ac311ec118")
NATIVE_TYPES = {"text", "equation", "shape", "table", "chart", "image", "svg", "media"}
SHAPES = {"rect", "rounded", "rounded-rectangle", "ellipse", "triangle", "arrow", "line", "path", "connector"}
LAYOUTS = {
    "free", "stack", "row", "grid", "two-column", "observation-interpretation", "equation-dissection",
    "figure-reading-guide", "claim-evidence-boundary", "before-gap-paper-view", "evaluation-protocol",
    "input-process-output", "two-column-contrast", "matrix-positioning-map", "custom",
}


class _InlineSanitizer(HTMLParser):
    allowed = {"b", "strong", "i", "em", "u", "s", "span", "br", "sub", "sup", "code", "a"}
    attrs = {"class", "style", "href", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []
        self.blocked = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            self.blocked += 1
            return
        if self.blocked:
            return
        if tag not in self.allowed:
            return
        safe = "".join(
            f' {name}="{html_lib.escape(value or "", quote=True)}"'
            for name, value in attrs
            if name.lower() in self.attrs and not (name.lower() == "href" and (value or "").lower().startswith("javascript:"))
        )
        self.parts.append(f"<{tag}{safe}>")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            self.blocked = max(0, self.blocked - 1)
        elif not self.blocked and tag in self.allowed and tag != "br":
            self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self.blocked:
            self.parts.append(html_lib.escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        if not self.blocked:
            self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if not self.blocked:
            self.parts.append(f"&#{name};")


def sanitize_inline_html(value: str) -> str:
    parser = _InlineSanitizer()
    parser.feed(value)
    parser.close()
    return "".join(parser.parts).strip()


def sanitize_svg_markup(value: str) -> str:
    value = re.sub(r"<script\b[^>]*>.*?</script\s*>", "", value, flags=re.IGNORECASE | re.DOTALL)
    value = re.sub(r"\s+on[a-z]+\s*=\s*([\"']).*?\1", "", value, flags=re.IGNORECASE | re.DOTALL)
    value = re.sub(r"\s+(?:href|xlink:href|src)\s*=\s*([\"'])javascript:.*?\1", "", value, flags=re.IGNORECASE | re.DOTALL)
    return value


def _normalize_positioned_svg(markup: str, width: float, height: float) -> str:
    """Remove HTML positioning from the outer SVG before Bento positions it."""

    match = re.search(r"<svg\b([^>]*)>", markup, flags=re.IGNORECASE)
    if not match:
        return markup
    attributes = match.group(1)
    attributes = re.sub(r"\s+(?:style|x|y|width|height)\s*=\s*([\"']).*?\1", "", attributes, flags=re.IGNORECASE | re.DOTALL)
    if not re.search(r"\sxmlns\s*=", attributes, flags=re.IGNORECASE):
        attributes += ' xmlns="http://www.w3.org/2000/svg"'
    opening = f'<svg{attributes} width="{_compact(width)}" height="{_compact(height)}">'
    return markup[:match.start()] + opening + markup[match.end():]


def _compact(value: float) -> int | float:
    rounded = round(float(value), 3)
    return int(rounded) if rounded.is_integer() else rounded


def _gradient(value: object) -> dict[str, Any] | None:
    if not isinstance(value, str) or "linear-gradient" not in value:
        return None
    angle_match = re.search(r"linear-gradient\(\s*(-?[\d.]+)deg", value)
    angle = float(angle_match.group(1)) if angle_match else 180.0
    colors = re.findall(r"rgba?\([^)]*\)|#[0-9a-fA-F]{3,8}", value)
    if len(colors) < 2:
        return None
    return {
        "angle": _compact(angle),
        "stops": [{"at": _compact(index / (len(colors) - 1)), "color": color} for index, color in enumerate(colors)],
    }


def _transparent(value: object) -> bool:
    normalized = str(value or "").replace(" ", "").lower()
    return normalized in {"", "transparent", "rgba(0,0,0,0)", "hsla(0,0%,0%,0)"}


def _frame(element: dict[str, Any], corrections: list[dict[str, Any]], slide_id: str) -> dict[str, Any]:
    original = {field: float(element[field]) for field in ("x", "y", "w", "h")}
    corrected = dict(original)
    overflow_growth = False
    corrected["w"] = max(1, min(corrected["w"], CANVAS_WIDTH))
    corrected["h"] = max(1, min(corrected["h"], CANVAS_HEIGHT))
    corrected["x"] = max(0, min(corrected["x"], CANVAS_WIDTH - corrected["w"]))
    corrected["y"] = max(0, min(corrected["y"], CANVAS_HEIGHT - corrected["h"]))
    if element.get("type") in {"text", "equation", "table", "chart"}:
        needed_w = min(max(corrected["w"], float(element.get("scrollWidth") or 0)), CANVAS_WIDTH)
        needed_h = min(max(corrected["h"], float(element.get("scrollHeight") or 0)), CANVAS_HEIGHT)
        if needed_w > corrected["w"]:
            overflow_growth = True
            corrected["x"] = max(0, min(corrected["x"], CANVAS_WIDTH - needed_w))
            corrected["w"] = needed_w
        if needed_h > corrected["h"]:
            overflow_growth = True
            corrected["y"] = max(0, min(corrected["y"], CANVAS_HEIGHT - needed_h))
            corrected["h"] = needed_h
    if corrected != original:
        corrections.append({
            "slideId": slide_id,
            "elementId": element["id"],
            "kind": "overflow-frame-growth" if overflow_growth else "bounds",
            "before": original,
            "after": corrected,
            "contentChanged": False,
        })
    return {field: _compact(value) for field, value in corrected.items()}


def _common(element: dict[str, Any], corrections: list[dict[str, Any]], slide_id: str) -> dict[str, Any]:
    result = {
        "id": element["id"],
        **_frame(element, corrections, slide_id),
        "rotation": _compact(element.get("rotation", 0)),
        "opacity": _compact(element.get("opacity", 1)),
    }
    for field in ("role", "morphId", "link"):
        if element.get(field):
            result[field] = element[field]
    shadow = element.get("style", {}).get("boxShadow")
    if shadow and shadow != "none":
        result["shadow"] = shadow
    return result


def _text(element: dict[str, Any], registry: dict[str, Any], corrections: list[dict[str, Any]], slide_id: str) -> dict[str, Any]:
    equation_id = element.get("equationId") or (element.get("registryId") if element.get("type") == "equation" else None)
    style = element["style"]
    font_size = max(1, float(style.get("fontSize") or 16))
    common = _common(element, corrections, slide_id)
    padding = {
        "left": float(style.get("paddingLeft") or 0), "right": float(style.get("paddingRight") or 0),
        "top": float(style.get("paddingTop") or 0), "bottom": float(style.get("paddingBottom") or 0),
    }
    if any(padding.values()) and common["w"] > padding["left"] + padding["right"] and common["h"] > padding["top"] + padding["bottom"]:
        before_padding = {field: common[field] for field in ("x", "y", "w", "h")}
        common["x"] = _compact(common["x"] + padding["left"])
        common["y"] = _compact(common["y"] + padding["top"])
        common["w"] = _compact(common["w"] - padding["left"] - padding["right"])
        common["h"] = _compact(common["h"] - padding["top"] - padding["bottom"])
        corrections.append({
            "slideId": slide_id, "elementId": element["id"], "kind": "text-padding-inset",
            "before": before_padding, "after": {field: common[field] for field in ("x", "y", "w", "h")}, "contentChanged": False,
        })
    content_scroll_height = max(0, float(element.get("scrollHeight") or 0) - padding["top"] - padding["bottom"])
    if content_scroll_height > common["h"] + 1:
        ratio = common["h"] / max(content_scroll_height, 1)
        adjusted = max(font_size * 0.8, font_size * ratio)
        if adjusted < font_size:
            corrections.append({
                "slideId": slide_id, "elementId": element["id"], "kind": "text-overflow",
                "before": {"fontSize": font_size}, "after": {"fontSize": _compact(adjusted)}, "contentChanged": False,
            })
            font_size = adjusted
    if equation_id:
        equation = registry["equations"].get(equation_id)
        if not equation:
            raise ConversionError(f"Equation {equation_id!r} referenced by {slide_id}/{element['id']} is missing from the registry.")
        latex = equation["latex"].strip()
        content = f"$${latex}$$"
    else:
        content = sanitize_inline_html(element.get("html", "")) or html_lib.escape(element.get("text", ""), quote=False)
    align = style.get("textAlign") if style.get("textAlign") in {"left", "center", "right", "justify"} else "left"
    valign = "middle" if style.get("verticalAlign") == "middle" else "top"
    if style.get("display") in {"flex", "inline-flex"}:
        if style.get("flexDirection") in {"column", "column-reverse"}:
            if style.get("alignItems") == "center":
                align = "center"
            if style.get("justifyContent") == "center":
                valign = "middle"
        else:
            if style.get("justifyContent") == "center":
                align = "center"
            if style.get("alignItems") == "center":
                valign = "middle"
    result = {
        **common,
        "type": "text",
        "html": content,
        "fontSize": _compact(font_size),
        "fontFamily": style.get("fontFamily") or "sans-serif",
        "fontWeight": style.get("fontWeight") or 400,
        "color": style.get("color") or "#111111",
        "align": align,
        "valign": valign,
        "lineHeight": _compact(max(0.1, float(style.get("lineHeight") or 1.2))),
        "letterSpacing": _compact(float(style.get("letterSpacing") or 0)),
    }
    if equation_id:
        result["equationId"] = equation_id
        result["latexSource"] = registry["equations"][equation_id]["latex"].strip()
    color_gradient = _gradient(style.get("backgroundImage"))
    if color_gradient and str(style.get("color", "")).lower() in {"rgba(0, 0, 0, 0)", "transparent"}:
        result["colorGradient"] = color_gradient
    return result


def _text_decoration(element: dict[str, Any], corrections: list[dict[str, Any]], slide_id: str) -> dict[str, Any] | None:
    style = element["style"]
    gradient = _gradient(style.get("backgroundImage"))
    if _transparent(style.get("backgroundColor")) and not gradient and float(style.get("borderWidth") or 0) <= 0:
        return None
    common = _common(element, corrections, slide_id)
    common["id"] = f"{element['id']}--decoration"
    common.pop("link", None)
    common.pop("role", None)
    common.pop("morphId", None)
    result = {
        **common, "type": "shape", "shape": "rect",
        "fill": style.get("backgroundColor") or "transparent",
        "stroke": style.get("borderColor") or "transparent",
        "strokeWidth": _compact(style.get("borderWidth") or 0),
        "radius": _compact(style.get("borderRadius") or 0),
    }
    if gradient:
        result["fillGradient"] = gradient
    return result


def _endpoint(value: str | None) -> dict[str, str] | None:
    if not value:
        return None
    element, _, side = value.partition(":")
    return {"el": element, "side": side or "center"}


def _shape(element: dict[str, Any], corrections: list[dict[str, Any]], slide_id: str) -> dict[str, Any]:
    source_shape = element.get("shape") or "rect"
    if source_shape not in SHAPES:
        raise ConversionError(f"Unsupported native shape {source_shape!r}")
    shape = "rect" if source_shape in {"rounded", "rounded-rectangle"} else "line" if source_shape == "connector" else source_shape
    style = element["style"]
    result = {
        **_common(element, corrections, slide_id),
        "type": "shape",
        "shape": shape,
        "fill": style.get("backgroundColor") or "transparent",
        "stroke": style.get("borderColor") or "transparent",
        "strokeWidth": _compact(style.get("borderWidth") or 0),
        "radius": _compact(style.get("borderRadius") or 0),
    }
    if shape == "line":
        line_color = style.get("borderTopColor") or style.get("borderColor") or style.get("backgroundColor")
        result["fill"] = line_color
        result["stroke"] = line_color
        result["strokeWidth"] = _compact(style.get("borderTopWidth") or style.get("borderWidth") or 1)
        source_stroke_style = element.get("strokeStyle") or style.get("borderTopStyle")
        if source_stroke_style in {"solid", "dashed", "dotted"}:
            result["strokeStyle"] = source_stroke_style
    gradient = _gradient(element["style"].get("backgroundImage"))
    if gradient:
        result["fillGradient"] = gradient
    for field in ("lineStart", "lineEnd"):
        if element.get(field):
            result[field] = element[field]
    if source_shape == "path":
        match = re.search(r'\bd=["\']([^"\']+)', element.get("outerHTML", ""))
        if match:
            result["d"] = match.group(1)
            result["pathBox"] = [0, 0, result["w"], result["h"]]
        for field, pattern in (
            ("fill", r'\bfill=["\']([^"\']+)'),
            ("stroke", r'\bstroke=["\']([^"\']+)'),
            ("strokeWidth", r'\bstroke-width=["\']([^"\']+)'),
        ):
            match = re.search(pattern, element.get("outerHTML", ""))
            if match:
                result[field] = _compact(float(match.group(1))) if field == "strokeWidth" else match.group(1)
    if source_shape == "connector":
        for field in ("from", "to"):
            endpoint = _endpoint(element.get(field))
            if endpoint:
                result[field] = endpoint
    return result


def _table(element: dict[str, Any], corrections: list[dict[str, Any]], slide_id: str) -> dict[str, Any]:
    data = element.get("table") or {"rows": []}
    rows = data.get("rows", [])
    header_rows = int(data.get("headerRows") or 0)
    column_count = len(rows[0]) if rows else 0
    widths = data.get("columnWidths") or [1] * column_count
    average_width = sum(widths) / len(widths) if widths else 1
    columns = [{"w": _compact(float(width) / max(average_width, 1))} for width in widths]
    native_rows = []
    for row in rows:
        cells = []
        for cell in row:
            if isinstance(cell, dict):
                converted_cell = {"html": sanitize_inline_html(str(cell.get("html", "")))}
                if cell.get("align") in {"left", "center", "right"}:
                    converted_cell["align"] = cell["align"]
                if cell.get("color"):
                    converted_cell["color"] = cell["color"]
                if cell.get("bg") and not _transparent(cell["bg"]):
                    converted_cell["bg"] = cell["bg"]
                if cell.get("bold"):
                    converted_cell["bold"] = True
            else:
                converted_cell = {"html": html_lib.escape(str(cell), quote=False)}
            cells.append(converted_cell)
        native_rows.append({"cells": cells})
    style = element["style"]
    return {
        **_common(element, corrections, slide_id), "type": "table",
        "columns": columns, "rows": native_rows,
        "header": header_rows > 0,
        "style": {
            "headerBg": style.get("backgroundColor") or "#eef2f7", "headerColor": style.get("color") or "#111111",
            "zebra": "rgba(0,0,0,0.03)", "borderColor": style.get("borderColor") or "#cbd5e1",
            "borderWidth": _compact(style.get("borderWidth") or 1), "cellPadX": 12, "cellPadY": 9,
            "fontSize": _compact(style.get("fontSize") or 16), "color": style.get("color") or "#111111",
            "radius": _compact(style.get("borderRadius") or 0),
        },
    }


def _chart(element: dict[str, Any], registry: dict[str, Any], corrections: list[dict[str, Any]], slide_id: str) -> dict[str, Any]:
    option = element.get("chartOption")
    if option is None and element.get("chartId"):
        source = registry["charts"].get(element["chartId"], {})
        option = source.get("option") if isinstance(source, dict) else None
    if not isinstance(option, dict):
        raise ConversionError("Native chart requires a structured JSON option.")
    option = json.loads(json.dumps(option, ensure_ascii=False))
    preset = option.pop("_bentoPreset", None) if "_bentoPreset" in option else option.pop("preset", None)
    if not preset:
        series = option.get("series", [])
        preset = series[0].get("type") if isinstance(series, list) and series and isinstance(series[0], dict) else "bar"
    preset = preset if preset in {"bar", "line", "pie", "scatter"} else "bar"
    return {**_common(element, corrections, slide_id), "type": "chart", "preset": preset, "option": option}


def _data_uri(path: Path, mime: str | None = None) -> str:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ConversionError(f"Cannot read asset {path}: {exc}") from exc
    media_type = mime or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return f"data:{media_type};base64,{base64.b64encode(payload).decode('ascii')}"


def _asset_source(asset_id: str, chapters: list[SourceChapter]) -> tuple[str, str]:
    for chapter in chapters:
        item = chapter.registry.get("assets", {}).get(asset_id)
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("data"), str):
            return item["data"], item.get("mimeType") or "application/octet-stream"
        if isinstance(item.get("path"), str):
            path = (chapter.registry_path.parent / item["path"]).resolve()
            mime = item.get("mimeType") or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            return _data_uri(path, mime), mime
    raise ConversionError(f"Asset {asset_id!r} is missing or has neither path nor data.")


def _slide_background(
    slide: dict[str, Any], assets: dict[str, Any], corrections: list[dict[str, Any]], resource_context: ResourceContext,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    compatibility = classify_slide_background(slide)
    if compatibility.classification == "native-safe":
        return [], None
    slide_id = slide["id"]
    style = slide.get("backgroundStyle", {})
    background_image = replace_css_urls(str(style.get("backgroundImage") or "none"), context=resource_context)
    common = {"id": f"{slide_id}--background", "x": 0, "y": 0, "w": CANVAS_WIDTH, "h": CANVAS_HEIGHT, "rotation": 0, "opacity": 1}
    if compatibility.classification == "native-with-adjustment" and background_image.startswith("linear-gradient("):
        gradient = _gradient(background_image)
        if gradient:
            output = {**common, "type": "shape", "shape": "rect", "fill": style.get("backgroundColor") or "transparent", "stroke": "none", "strokeWidth": 0, "radius": 0, "fillGradient": gradient}
            return [output], {
                "slideId": slide_id, "elementId": common["id"], "sourceType": "slide-background", "resultType": "shape", "bentoType": "shape",
                "emittedIds": [common["id"]], "strategy": "native-decomposition", "conversionMode": "native-decomposition",
                "reason": compatibility.adjustments[0], "role": "slide-background", "layout": slide.get("layout"), "layoutGroup": None,
                "sourceFrame": {"x": 0, "y": 0, "w": CANVAS_WIDTH, "h": CANVAS_HEIGHT}, "bentoFrame": {"x": 0, "y": 0, "w": CANVAS_WIDTH, "h": CANVAS_HEIGHT},
                "nativeCompatibility": compatibility.classification, "compatibilityReasons": [], "contentPreserved": True,
                "adjustments": list(compatibility.adjustments), "warnings": [],
            }
    url_match = re.fullmatch(r'url\(["\']?(.*?)["\']?\)', background_image)
    if compatibility.classification == "native-with-adjustment" and url_match:
        source = resolve_embedded_resource(url_match.group(1), context=resource_context)
        fit = "cover" if style.get("backgroundSize") == "cover" else "contain" if style.get("backgroundSize") == "contain" else "fill"
        output = {**common, "type": "image", "src": source, "fit": fit, "radius": 0}
        return [output], {
            "slideId": slide_id, "elementId": common["id"], "sourceType": "slide-background", "resultType": "image", "bentoType": "image",
            "emittedIds": [common["id"]], "strategy": "native-decomposition", "conversionMode": "native-decomposition",
            "reason": compatibility.adjustments[0], "role": "slide-background", "layout": slide.get("layout"), "layoutGroup": None,
            "sourceFrame": {"x": 0, "y": 0, "w": CANVAS_WIDTH, "h": CANVAS_HEIGHT}, "bentoFrame": {"x": 0, "y": 0, "w": CANVAS_WIDTH, "h": CANVAS_HEIGHT},
            "nativeCompatibility": compatibility.classification, "compatibilityReasons": [], "contentPreserved": True,
            "adjustments": list(compatibility.adjustments), "warnings": [],
        }
    css = ";".join([
        "width:100%", "height:100%", f"background-color:{style.get('backgroundColor') or 'transparent'}",
        f"background-image:{background_image}", f"background-size:{style.get('backgroundSize') or 'auto'}",
        f"background-position:{style.get('backgroundPosition') or '0% 0%'}", f"background-repeat:{style.get('backgroundRepeat') or 'repeat'}",
    ])
    markup = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720">'
        f'<foreignObject width="100%" height="100%"><div xmlns="http://www.w3.org/1999/xhtml" style="{html_lib.escape(css, quote=True)}"></div></foreignObject></svg>'
    )
    output = {**common, "type": "svg", "markup": markup}
    return [output], {
        "slideId": slide_id, "elementId": common["id"], "sourceType": "slide-background", "resultType": "svg", "bentoType": "svg",
        "emittedIds": [common["id"]], "strategy": "svg", "conversionMode": "background-svg-fallback",
        "reason": compatibility.reasons[0], "role": "slide-background", "layout": slide.get("layout"), "layoutGroup": None,
        "sourceFrame": {"x": 0, "y": 0, "w": CANVAS_WIDTH, "h": CANVAS_HEIGHT}, "bentoFrame": {"x": 0, "y": 0, "w": CANVAS_WIDTH, "h": CANVAS_HEIGHT},
        "nativeCompatibility": compatibility.classification, "compatibilityReasons": list(compatibility.reasons), "contentPreserved": True,
        "adjustments": [], "warnings": list(compatibility.reasons),
    }


def _svg_fallback(
    element: dict[str, Any], corrections: list[dict[str, Any]], slide_id: str,
    resource_context: ResourceContext, capture: str | None = None,
) -> dict[str, Any]:
    if capture and (element.get("transform", {}).get("hasSkew") or element.get("transform", {}).get("is3D")):
        bounding = element.get("boundingFrame") or {field: element[field] for field in ("x", "y", "w", "h")}
        fallback_element = {**element, **bounding, "rotation": 0}
        width, height = max(1, float(bounding["w"])), max(1, float(bounding["h"]))
        markup = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
            f'<image href="{html_lib.escape(capture, quote=True)}" width="{width}" height="{height}" preserveAspectRatio="none"/></svg>'
        )
        return {**_common(fallback_element, corrections, slide_id), "type": "svg", "markup": markup}
    width = max(1, float(element["w"]))
    height = max(1, float(element["h"]))
    # A complex HTML block may contain a direct child SVG alongside other
    # content. Treat only semantic SVG elements as standalone SVG markup.
    markup = element.get("svg") if element.get("type") == "svg" else None
    if markup:
        markup = _normalize_positioned_svg(markup, width, height)
        markup = embed_markup_resources(markup, context=resource_context)
    if not markup:
        style = element.get("style", {})
        body = embed_markup_resources(element.get("html", "") or html_lib.escape(element.get("text", ""), quote=False), context=resource_context)
        css = ";".join([
            "box-sizing:border-box", "width:100%", "height:100%",
            f"color:{style.get('color') or '#111'}",
            f"background-color:{style.get('backgroundColor') or 'transparent'}",
            f"background-image:{style.get('backgroundImage') or 'none'}",
            f"border:{style.get('borderWidth') or 0}px solid {style.get('borderColor') or 'transparent'}",
            f"border-radius:{style.get('borderRadius') or 0}px",
            f"padding:{style.get('padding') or '0px'}",
            f"font-family:{style.get('fontFamily') or 'sans-serif'}",
            f"font-size:{style.get('fontSize') or 16}px",
            f"font-weight:{style.get('fontWeight') or 400}",
            f"line-height:{style.get('lineHeight') or 1.2}",
            f"text-align:{style.get('textAlign') or 'left'}",
            f"box-shadow:{style.get('boxShadow') or 'none'}",
            f"filter:{style.get('filter') or 'none'}",
            f"backdrop-filter:{style.get('backdropFilter') or 'none'}",
            f"clip-path:{style.get('clipPath') or 'none'}",
            f"mask-image:{style.get('maskImage') or 'none'}",
            f"mix-blend-mode:{style.get('mixBlendMode') or 'normal'}",
            f"background-size:{style.get('backgroundSize') or 'auto'}",
            f"background-position:{style.get('backgroundPosition') or '0% 0%'}",
            f"background-repeat:{style.get('backgroundRepeat') or 'repeat'}",
            f"display:{style.get('display') or 'block'}",
            f"justify-content:{style.get('justifyContent') or 'normal'}",
            f"align-items:{style.get('alignItems') or 'normal'}",
        ])
        markup = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
            f'<foreignObject width="100%" height="100%"><div xmlns="http://www.w3.org/1999/xhtml" style="{html_lib.escape(css, quote=True)}">{body}</div></foreignObject></svg>'
        )
        markup = embed_markup_resources(markup, context=resource_context)
    return {**_common(element, corrections, slide_id), "type": "svg", "markup": sanitize_svg_markup(markup)}


def _complex_table_svg(element: dict[str, Any], corrections: list[dict[str, Any]], slide_id: str, resource_context: ResourceContext) -> dict[str, Any]:
    width, height = max(1, float(element["w"])), max(1, float(element["h"]))
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">']
    for row in element.get("table", {}).get("rows", []):
        for cell in row:
            rect = cell.get("rect", {})
            x, y, w, h = (float(rect.get(field, 0)) for field in ("x", "y", "w", "h"))
            style = cell.get("style", {})
            fill_value = style.get("backgroundColor") or cell.get("bg")
            fill = fill_value if fill_value and not _transparent(fill_value) else "#ffffff"
            color = style.get("color") or cell.get("color") or "#111111"
            weight = 700 if cell.get("bold") else 400
            content = sanitize_svg_markup(embed_markup_resources(str(cell.get("html", "")), context=resource_context))
            font_family = style.get("fontFamily") or "Arial,sans-serif"
            font_size = _compact(float(style.get("fontSize") or 16))
            text_align = style.get("textAlign") or cell.get("align") or "left"
            parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{html_lib.escape(fill, quote=True)}" stroke="#94a3b8" stroke-width="1"/>')
            parts.append(
                f'<foreignObject x="{x + 8}" y="{y + 6}" width="{max(1, w - 16)}" height="{max(1, h - 12)}">'
                f'<div xmlns="http://www.w3.org/1999/xhtml" style="font-family:{html_lib.escape(str(font_family), quote=True)};font-size:{font_size}px;font-weight:{weight};color:{html_lib.escape(color, quote=True)};text-align:{html_lib.escape(str(text_align), quote=True)}">{content}</div></foreignObject>'
            )
    parts.append("</svg>")
    return {**_common(element, corrections, slide_id), "type": "svg", "markup": "".join(parts)}


def _identity_layout(slides: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    """Exclude machine-local URLs and browser-only diagnostics from the stable id input."""

    element_fields = (
        "id", "type", "exportMode", "critical", "compareCrop", "role", "registryId", "equationId", "assetId", "figureId",
        "chartId", "tableId", "morphId", "link", "shape", "lineStart", "lineEnd", "from", "to",
        "layout", "layoutGroup", "x", "y", "w", "h", "rotation", "opacity", "z", "text", "html",
        "svg", "mediaKind", "chartOption", "table", "style", "boundingFrame", "transform", "compatibility",
    )
    return [
        {
            "id": slide["id"], "name": slide.get("name"), "transition": slide.get("transition"),
            "stateOf": slide.get("stateOf"), "layout": slide.get("layout"), "background": slide.get("background"),
            "backgroundStyle": slide.get("backgroundStyle"),
            "notes": slide.get("notes"),
            "elements": [{field: element.get(field) for field in element_fields if element.get(field) is not None} for element in slide["elements"]],
        }
        for slide in slides
    ]


def _rotated_bounding_frame(element: dict[str, Any]) -> dict[str, float | int]:
    """Return the axis-aligned frame occupied by a rotated Bento element."""

    angle = math.radians(float(element.get("rotation") or 0))
    width, height = float(element["w"]), float(element["h"])
    bounding_width = abs(width * math.cos(angle)) + abs(height * math.sin(angle))
    bounding_height = abs(width * math.sin(angle)) + abs(height * math.cos(angle))
    center_x = float(element["x"]) + width / 2
    center_y = float(element["y"]) + height / 2
    return {
        "x": _compact(center_x - bounding_width / 2),
        "y": _compact(center_y - bounding_height / 2),
        "w": _compact(bounding_width),
        "h": _compact(bounding_height),
    }


def _rgb(value: object) -> tuple[float, float, float] | None:
    text = str(value or "")
    match = re.match(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)", text)
    if match:
        return tuple(float(match.group(index)) / 255 for index in range(1, 4))  # type: ignore[return-value]
    match = re.fullmatch(r"#([0-9a-fA-F]{6})", text)
    if match:
        raw = match.group(1)
        return tuple(int(raw[index:index + 2], 16) / 255 for index in (0, 2, 4))  # type: ignore[return-value]
    return None


def _luminance(rgb: tuple[float, float, float]) -> float:
    channels = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4 for channel in rgb]
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast(first: object, second: object) -> float | None:
    left, right = _rgb(first), _rgb(second)
    if left is None or right is None:
        return None
    high, low = sorted((_luminance(left), _luminance(right)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _correct_contrast(slides: list[dict[str, Any]], corrections: list[dict[str, Any]]) -> None:
    for slide in slides:
        by_id = {element["id"]: element for element in slide["elements"]}
        for element in slide["elements"]:
            if element.get("type") != "text":
                continue
            decoration = by_id.get(f"{element['id']}--decoration")
            background = decoration.get("fill") if decoration else slide["background"]
            ratio = _contrast(element.get("color"), background)
            if ratio is None or ratio >= 3:
                continue
            black = _contrast("#000000", background) or 0
            white = _contrast("#ffffff", background) or 0
            replacement = "#000000" if black >= white else "#ffffff"
            before = element["color"]
            element["color"] = replacement
            corrections.append({
                "slideId": slide["id"], "elementId": element["id"], "kind": "low-contrast",
                "before": {"color": before, "ratio": round(ratio, 3)},
                "after": {"color": replacement, "ratio": round(max(black, white), 3)}, "contentChanged": False,
            })


def _overlap_ratio(left: dict[str, Any], right: dict[str, Any]) -> float:
    overlap_w = min(left["x"] + left["w"], right["x"] + right["w"]) - max(left["x"], right["x"])
    overlap_h = min(left["y"] + left["h"], right["y"] + right["h"]) - max(left["y"], right["y"])
    if overlap_w <= 4 or overlap_h <= 4:
        return 0
    smaller = min(left["w"] * left["h"], right["w"] * right["h"])
    return overlap_w * overlap_h / max(smaller, 1)


def _overlap_pairs(elements: list[dict[str, Any]]) -> set[tuple[str, str]]:
    movable = {"text", "table", "chart"}
    return {
        tuple(sorted((left["id"], right["id"])))
        for index, left in enumerate(elements)
        for right in elements[index + 1:]
        if left.get("type") in movable and right.get("type") in movable and _overlap_ratio(left, right) > 0.15
    }


def _horizontal_separation(
    left: dict[str, Any], right: dict[str, Any], *, boundary: float | None = None, gap: float = 12,
) -> bool:
    first, second = left, right
    if boundary is not None:
        first["w"] = _compact(max(24, min(first["w"], boundary - gap / 2 - first["x"])))
        second["x"] = _compact(max(second["x"], boundary + gap / 2))
        second["w"] = _compact(min(second["w"], CANVAS_WIDTH - second["x"]))
    if first["x"] + first["w"] + gap > second["x"]:
        available = second["x"] - gap - first["x"]
        if available >= 24:
            first["w"] = _compact(available)
        else:
            second["x"] = _compact(first["x"] + first["w"] + gap)
            second["w"] = _compact(min(second["w"], CANVAS_WIDTH - second["x"]))
    return second["x"] >= first["x"] + first["w"] + gap - 0.01 and second["w"] >= 24


def _vertical_separation(top: dict[str, Any], bottom: dict[str, Any], gap: float = 12) -> bool:
    first, second = top, bottom
    if first["y"] + first["h"] + gap > second["y"]:
        available = second["y"] - gap - first["y"]
        if available >= 24:
            first["h"] = _compact(available)
        else:
            second["y"] = _compact(first["y"] + first["h"] + gap)
            second["h"] = _compact(min(second["h"], CANVAS_HEIGHT - second["y"]))
    return second["y"] >= first["y"] + first["h"] + gap - 0.01 and second["h"] >= 24


def _correct_overlaps(
    slides: list[dict[str, Any]], source_slides: tuple[dict[str, Any], ...],
    corrections: list[dict[str, Any]], diagnostics: list[dict[str, Any]],
) -> None:
    source_by_slide = {slide["id"]: slide for slide in source_slides}
    for slide in slides:
        source_slide = source_by_slide[slide["id"]]
        source_elements = {element["id"]: element for element in source_slide["elements"]}
        elements = slide["elements"]
        original_pairs = _overlap_pairs(elements)
        diagnosed: set[tuple[str, str]] = set()
        for pair in sorted(original_pairs):
            left = next(element for element in elements if element["id"] == pair[0])
            right = next(element for element in elements if element["id"] == pair[1])
            left_source, right_source = source_elements.get(left["id"]), source_elements.get(right["id"])
            layout_name = source_slide.get("layout") or "free"
            same_group = left_source and right_source and left_source.get("layoutGroup") and left_source.get("layoutGroup") == right_source.get("layoutGroup")
            context = {
                "layout": layout_name,
                "roles": [(left_source or {}).get("role"), (right_source or {}).get("role")],
                "layoutGroups": [(left_source or {}).get("layoutGroup"), (right_source or {}).get("layoutGroup")],
                "zOrder": [left.get("z"), right.get("z")],
                "sourceOrder": [(left_source or {}).get("domIndex"), (right_source or {}).get("domIndex")],
            }
            policy = None
            reason = None
            before = {element["id"]: {field: element[field] for field in ("x", "y", "w", "h")} for element in (left, right)}
            source_left_first = float((left_source or {}).get("x", left["x"])) <= float((right_source or {}).get("x", right["x"]))
            source_top_first = float((left_source or {}).get("y", left["y"])) <= float((right_source or {}).get("y", right["y"]))
            horizontal_pair = (left, right) if source_left_first else (right, left)
            vertical_pair = (left, right) if source_top_first else (right, left)
            if layout_name in {"two-column", "two-column-contrast"}:
                policy = "two-column-preserve-side"
                reason = "Both elements stay on their original side; widths are reduced before any position move."
                corrected = _horizontal_separation(*horizontal_pair, boundary=CANVAS_WIDTH / 2)
            elif layout_name == "observation-interpretation":
                policy = "observation-arrow-interpretation-order"
                reason = "Observation remains left of interpretation and the arrow row is unchanged."
                observation = left if (left_source or {}).get("role") == "observation" else right if (right_source or {}).get("role") == "observation" else horizontal_pair[0]
                interpretation = right if observation is left else left
                corrected = _horizontal_separation(observation, interpretation)
            elif layout_name == "equation-dissection":
                equation = left if (left_source or {}).get("type") == "equation" else right if (right_source or {}).get("type") == "equation" else None
                policy = "equation-above-explanations"
                reason = "The equation remains above explanation blocks; left/right explanation order is retained."
                corrected = _vertical_separation(equation, right if equation is left else left) if equation else _horizontal_separation(*horizontal_pair)
            elif layout_name == "row":
                policy = "row-preserve-horizontal-order"
                reason = "Horizontal DOM/source order is retained by shrinking width and restoring the gap."
                corrected = _horizontal_separation(*horizontal_pair)
            elif layout_name == "stack":
                policy = "stack-preserve-vertical-order"
                reason = "Vertical source order is retained and the lower element moves only downward."
                corrected = _vertical_separation(*vertical_pair)
            elif same_group and layout_name not in {"free", "custom"}:
                horizontal = abs(left_source["x"] - right_source["x"]) >= abs(left_source["y"] - right_source["y"])
                policy = "layout-group-preserve-source-axis"
                reason = "The shared layout group keeps its dominant source axis and ordering."
                corrected = _horizontal_separation(*horizontal_pair) if horizontal else _vertical_separation(*vertical_pair)
            else:
                corrected = False
                policy = "diagnostic-intent-uncertain"
                layering_signal = left.get("z") != right.get("z") or any(context["roles"])
                reason = (
                    "Free/custom role or z-order indicates possible intentional layering; no safe composition-preserving move is proven."
                    if layering_signal else
                    "Free/custom overlap may be intentional; role, group, z-order, and layout do not prove a safe move."
                )

            new_pairs = _overlap_pairs(elements)
            creates_new_overlap = bool(new_pairs - original_pairs)
            if not corrected or creates_new_overlap or pair in new_pairs:
                for element in (left, right):
                    element.update(before[element["id"]])
                diagnostics.append({
                    "slideId": slide["id"], "kind": "overlap", "elements": list(pair),
                    "ratio": round(_overlap_ratio(left, right), 3), "autoCorrected": False,
                    "policy": policy, "reason": reason,
                    "context": context,
                    "reinspection": "rolled back" if creates_new_overlap else "unresolved",
                })
                diagnosed.add(pair)
                continue
            after = {element["id"]: {field: element[field] for field in ("x", "y", "w", "h")} for element in (left, right)}
            corrections.append({
                "slideId": slide["id"], "elementId": right["id"], "kind": "overlap",
                "elementIds": list(pair),
                "policy": policy, "reason": reason, "before": before, "after": after,
                "context": context, "contentChanged": False, "reinspection": "passed-no-new-major-overlap",
            })

        for pair in sorted(_overlap_pairs(elements)):
            if pair not in diagnosed:
                left = next(element for element in elements if element["id"] == pair[0])
                right = next(element for element in elements if element["id"] == pair[1])
                diagnostics.append({
                    "slideId": slide["id"], "kind": "overlap", "elements": list(pair),
                    "ratio": round(_overlap_ratio(left, right), 3), "autoCorrected": False,
                    "policy": "post-correction-reinspection", "reason": "Major overlap remains after all safe layout-aware attempts.",
                    "reinspection": "unresolved",
                })


@dataclass(frozen=True)
class HtmlConversionResult:
    document: dict[str, Any]
    report: dict[str, Any]


def convert_html_layout(
    layout: LayoutResult,
    registry: dict[str, Any],
    chapters: list[SourceChapter],
) -> HtmlConversionResult:
    errors: list[str] = []
    corrections: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    asset_resolutions: list[dict[str, object]] = []
    seen_slides: set[str] = set()
    seen_elements: set[str] = set()
    assets: dict[str, Any] = {}
    slides: list[dict[str, Any]] = []
    chapters_by_id = {chapter.chapter_id: chapter for chapter in chapters}

    def asset_lookup(asset_id: str) -> str:
        return _asset_source(asset_id, chapters)[0]

    for source_slide in layout.slides:
        slide_id = source_slide["id"]
        if not slide_id or slide_id in seen_slides:
            errors.append(issue(slide_id=slide_id, field="data-slide-id", actual=slide_id, fix="Use a non-empty globally unique slide id."))
            continue
        seen_slides.add(slide_id)
        chapter_id = source_slide.get("chapterId")
        chapter = chapters_by_id.get(chapter_id) if chapter_id else (chapters[0] if len(chapters) == 1 else None)
        if chapter is None:
            errors.append(issue(slide_id=slide_id, field="chapterId", actual=chapter_id, fix="Keep source layout associated with a discovered HTML chapter."))
            continue
        resource_context = ResourceContext(
            source_html_path=chapter.html_path,
            slide_id=slide_id,
            element_id=f"{slide_id}--background",
            asset_prefix=f"html-{slide_id}",
            records=asset_resolutions,
            asset_lookup=asset_lookup,
        )
        if source_slide.get("layout") and source_slide["layout"] not in LAYOUTS:
            errors.append(issue(slide_id=slide_id, field="data-layout", actual=source_slide["layout"], fix=f"Use one of {sorted(LAYOUTS)}."))
        output_elements, background_decision = _slide_background(source_slide, assets, corrections, resource_context)
        if background_decision:
            decisions.append(background_decision)
        local_ids: set[str] = set()
        ordered = sorted(source_slide["elements"], key=lambda item: (item.get("z", 0), item["domIndex"]))
        for element in ordered:
            element_id = element["id"]
            key = f"{slide_id}/{element_id}"
            element_resource_context = resource_context.for_element(element_id, f"html-{slide_id}-{element_id}")
            if element_id in local_ids:
                errors.append(issue(slide_id=slide_id, element_id=element_id, field="data-bento-id", actual=element_id, fix="Use an id unique within the slide."))
                continue
            local_ids.add(element_id)
            seen_elements.add(element_id)
            references = {
                "equationId": "equations",
                "assetId": "assets",
                "figureId": "figures",
                "tableId": "tables",
                "chartId": "charts",
            }
            for field, collection in references.items():
                reference = element.get(field)
                if reference and reference not in registry[collection]:
                    errors.append(issue(slide_id=slide_id, element_id=element_id, field=field, actual=reference, fix=f"Define this id in registry.{collection}."))
            mode = element.get("exportMode") or "auto"
            compatibility = classify_native_compatibility(element)
            if mode not in {"native", "svg", "image", "auto", "ignore"}:
                errors.append(issue(slide_id=slide_id, element_id=element_id, field="data-bento-export", actual=mode, fix="Use native, svg, image, auto, or ignore."))
                continue
            if mode == "ignore":
                decisions.append({"slideId": slide_id, "elementId": element_id, "sourceType": element["type"], "resultType": None, "bentoType": None, "strategy": "ignore", "conversionMode": "ignore", "reason": "Explicit data-bento-export=ignore", "adjustments": [], "warnings": []})
                continue
            strategy = "native"
            reason = "Supported semantic element converted to an editable Bento native type."
            try:
                if mode == "image" or compatibility.classification == "image-required":
                    source = layout.image_fallbacks.get(key)
                    if not source:
                        raise ConversionError("Image fallback capture is unavailable.")
                    asset_id = f"fallback-{slide_id}-{element_id}"
                    assets[asset_id] = source
                    converted = {**_common(element, corrections, slide_id), "type": "image", "src": source, "fit": "contain", "radius": 0}
                    strategy = "image"
                    reason = "Explicit data-bento-export=image raster fallback." if mode == "image" else "; ".join(compatibility.reasons)
                elif mode == "svg" or compatibility.classification == "localized-svg-recommended" or element["type"] not in NATIVE_TYPES:
                    capture = layout.image_fallbacks.get(key)
                    converted = _complex_table_svg(element, corrections, slide_id, element_resource_context) if element["type"] == "table" and element.get("table") and not element["table"].get("simpleTable", True) else _svg_fallback(element, corrections, slide_id, element_resource_context, capture)
                    strategy = "svg"
                    reason = "Explicit data-bento-export=svg localized fallback." if mode == "svg" else "; ".join(compatibility.reasons) or "Unsupported complex block preserved as partial SVG."
                elif element["type"] in {"text", "equation"}:
                    converted = _text(element, registry, corrections, slide_id)
                elif element["type"] == "shape":
                    converted = _shape(element, corrections, slide_id)
                elif element["type"] == "table":
                    converted = _table(element, corrections, slide_id)
                elif element["type"] == "chart":
                    converted = _chart(element, registry, corrections, slide_id)
                elif element["type"] == "image":
                    source = element.get("src")
                    asset_id = element.get("assetId")
                    if asset_id:
                        source = resolve_embedded_resource(f"asset:{asset_id}", context=element_resource_context)
                        assets[asset_id] = source
                    elif source:
                        source = resolve_embedded_resource(source, context=element_resource_context)
                    if not source:
                        raise ConversionError("Image has no src or registry asset id.")
                    fit = element["style"].get("objectFit")
                    converted = {**_common(element, corrections, slide_id), "type": "image", "src": source, "fit": fit if fit in {"cover", "contain", "fill"} else "contain", "radius": _compact(element["style"].get("borderRadius") or 0)}
                elif element["type"] == "svg":
                    converted = _svg_fallback(element, corrections, slide_id, element_resource_context)
                elif element["type"] == "media":
                    media_source = element.get("src")
                    if element.get("assetId"):
                        media_source = resolve_embedded_resource(f"asset:{element['assetId']}", context=element_resource_context)
                        assets[element["assetId"]] = media_source
                    elif media_source:
                        media_source = resolve_embedded_resource(media_source, context=element_resource_context)
                    if not media_source:
                        raise ConversionError("Media has no src.")
                    converted = {**_common(element, corrections, slide_id), "type": "media", "kind": element.get("mediaKind", "video"), "src": media_source, "controls": element.get("controls", True), "autoplay": element.get("autoplay", False), "loop": element.get("loop", False), "muted": element.get("muted", False), "fit": "contain", "radius": _compact(element["style"].get("borderRadius") or 0)}
                else:
                    raise ConversionError(f"Unsupported source type {element['type']!r}")
            except ConversionError as exc:
                if mode == "native":
                    converted = _svg_fallback(element, corrections, slide_id, element_resource_context, layout.image_fallbacks.get(key))
                    strategy, reason = "svg", f"Native conversion failed; partial SVG fallback: {exc}"
                else:
                    converted = _svg_fallback(element, corrections, slide_id, element_resource_context, layout.image_fallbacks.get(key))
                    strategy, reason = "svg", f"Automatic native conversion failed; partial SVG fallback: {exc}"
            emitted = [converted]
            if strategy == "native" and element["type"] in {"text", "equation"}:
                decoration = _text_decoration(element, corrections, slide_id)
                if decoration:
                    emitted.insert(0, decoration)
                    strategy = "native-decomposition"
                    reason = "Semantic text stayed editable; computed background/border became a native shape."
            if strategy in {"native", "native-decomposition"} and compatibility.classification == "native-with-adjustment":
                reason = " ".join(compatibility.adjustments)
            output_elements.extend(emitted)
            conversion_mode = {"svg": "partial-svg-fallback", "image": "image-fallback", "native-decomposition": "native-decomposition"}.get(strategy, "native")
            decisions.append({
                "slideId": slide_id, "elementId": element_id, "sourceType": element["type"],
                "resultType": converted["type"], "bentoType": [item["type"] for item in emitted] if len(emitted) > 1 else converted["type"],
                "emittedIds": [item["id"] for item in emitted], "strategy": strategy, "conversionMode": conversion_mode,
                "reason": reason, "layout": element.get("layout"), "layoutGroup": element.get("layoutGroup"),
                "role": element.get("role"), "sourceTag": element.get("tag"),
                "critical": bool(element.get("critical")),
                "compareCrop": bool(element.get("compareCrop")),
                "paperSource": element.get("paperSource"),
                "sourceFrame": {field: _compact(element[field]) for field in ("x", "y", "w", "h")},
                "sourceBoundingFrame": {field: _compact((element.get("boundingFrame") or element)[field]) for field in ("x", "y", "w", "h")},
                "nativeCompatibility": compatibility.classification,
                "compatibilityReasons": list(compatibility.reasons),
                "contentPreserved": True, "adjustments": list(compatibility.adjustments), "warnings": [reason] if strategy in {"svg", "image"} else [],
            })
        slides.append({
            "id": slide_id,
            "background": source_slide.get("background") or "#ffffff",
            "transition": source_slide.get("transition") or "none",
            "notes": source_slide.get("notes") or "",
            "elements": output_elements,
            **({"name": source_slide["name"]} if source_slide.get("name") else {}),
            **({"stateOf": source_slide["stateOf"]} if source_slide.get("stateOf") else {}),
        })

    protected = registry["protected"]
    for protected_id in protected["slideIds"]:
        if protected_id not in seen_slides:
            errors.append(issue(slide_id=protected_id, field="protected.slideIds", actual=None, fix="Restore the protected slide in HTML."))
    for protected_id in protected["elementIds"]:
        if protected_id not in seen_elements:
            errors.append(issue(element_id=protected_id, field="protected.elementIds", actual=None, fix="Restore the protected element in HTML."))
    source_text = "\n".join(element.get("text", "") for slide in layout.slides for element in slide["elements"])
    for required in protected["requiredText"]:
        if required not in source_text:
            errors.append(issue(field="protected.requiredText", actual=required, fix="Restore the required source text without rewriting it."))

    slide_id_set = {slide["id"] for slide in slides}
    for slide_index, slide in enumerate(slides):
        if slide.get("stateOf") and slide["stateOf"] not in slide_id_set:
            errors.append(issue(slide_id=slide["id"], field="stateOf", actual=slide["stateOf"], fix="Reference an existing slide id."))
        ids = {element["id"] for element in slide["elements"]}
        for element in slide["elements"]:
            link = element.get("link")
            if isinstance(link, str) and link and not re.match(r"^(?:https?://|mailto:|#)", link) and link not in slide_id_set:
                errors.append(issue(slide_id=slide["id"], element_id=element["id"], field="link", actual=link, fix="Reference an existing slide/state id or an explicit external URL."))
            if element.get("type") == "shape":
                for endpoint_name in ("from", "to"):
                    endpoint = element.get(endpoint_name)
                    if endpoint and endpoint.get("el") not in ids:
                        errors.append(issue(slide_id=slide["id"], element_id=element["id"], field=endpoint_name, actual=endpoint, fix="Reference an element on the same slide."))
        if slide.get("transition") == "morph":
            previous = slides[slide_index - 1] if slide_index else None
            previous_keys = {
                item.get("morphId") or item["id"] for item in previous["elements"]
            } if previous else set()
            current_keys = {item.get("morphId") or item["id"] for item in slide["elements"]}
            if not previous_keys.intersection(current_keys):
                errors.append(issue(slide_id=slide["id"], field="transition", actual="morph", fix="Share at least one stable element id or morphId with the previous slide."))
    if errors:
        raise ValidationError(errors)

    document_meta = registry.get("document", {})
    canonical = json.dumps({"registry": registry, "slides": _identity_layout(layout.slides)}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    document = {
        "format": "bento/slides",
        "version": 1,
        "docId": document_meta.get("docId") or str(uuid.uuid5(DOC_ID_NAMESPACE, canonical)),
        "title": document_meta.get("title") or "HTML-first Bento presentation",
        "size": {"width": CANVAS_WIDTH, "height": CANVAS_HEIGHT},
        "theme": document_meta.get("theme") or {"background": "#ffffff", "color": "#111111", "accent": "#2563eb", "fontFamily": "Arial"},
        "slides": slides,
        "modified": document_meta.get("modified") or "1970-01-01T00:00:00Z",
    }
    if assets:
        document["assets"] = assets
    if registry.get("fonts"):
        fonts = []
        for font_id, font in registry["fonts"].items():
            if not isinstance(font, dict):
                continue
            asset_id = font.get("asset")
            if asset_id:
                font_context = ResourceContext(
                    source_html_path=chapters[0].html_path, slide_id="<document>", element_id=f"font:{font_id}",
                    asset_prefix=f"font-{font_id}", records=asset_resolutions, asset_lookup=asset_lookup,
                )
                source = resolve_embedded_resource(f"asset:{asset_id}", context=font_context)
                assets[asset_id] = source
            fonts.append({key: value for key, value in font.items() if key in {"family", "asset", "weight"}})
        document["fonts"] = fonts
        if assets:
            document["assets"] = assets

    resource_scan = scan_document_resources(document)

    _correct_contrast(slides, corrections)
    _correct_overlaps(slides, layout.slides, corrections, diagnostics)

    output_by_key = {
        f"{slide['id']}/{element['id']}": element
        for slide in slides for element in slide["elements"]
    }
    corrections_by_key: dict[str, list[dict[str, Any]]] = {}
    for correction in corrections:
        corrections_by_key.setdefault(f"{correction['slideId']}/{correction['elementId']}", []).append(correction)
    for decision in decisions:
        key = f"{decision['slideId']}/{decision['elementId']}"
        output_element = output_by_key.get(key)
        if output_element:
            decision["bentoFrame"] = {field: output_element[field] for field in ("x", "y", "w", "h")}
            decision["bentoBoundingFrame"] = _rotated_bounding_frame(output_element)
            decision["styleChecks"] = {
                "color": output_element.get("color") or output_element.get("fill"),
                "fontSize": output_element.get("fontSize"),
                "zOrderPreserved": True,
            }
        decision["adjustments"] = list(decision.get("adjustments", [])) + [
            f"{item['kind']}: {item['before']} -> {item['after']}"
            for item in corrections_by_key.get(key, [])
        ]

    counts: dict[str, int] = {}
    for decision in decisions:
        counts[decision["strategy"]] = counts.get(decision["strategy"], 0) + 1
    compatibility_counts: dict[str, int] = {}
    for decision in decisions:
        classification = decision.get("nativeCompatibility", "not-applicable")
        compatibility_counts[classification] = compatibility_counts.get(classification, 0) + 1
    fallback_ids = {
        f"{decision['slideId']}/{emitted_id}"
        for decision in decisions if decision["strategy"] in {"svg", "image"}
        for emitted_id in decision.get("emittedIds", [])
    }
    native_counts = {kind: 0 for kind in ("text", "shape", "table", "chart", "image", "svg", "media")}
    for slide in slides:
        for element in slide["elements"]:
            if f"{slide['id']}/{element['id']}" not in fallback_ids and element["type"] in native_counts:
                native_counts[element["type"]] += 1
    summary = {
        "slides": len(slides), "elements": sum(len(slide["elements"]) for slide in slides), "sourceElements": len(decisions),
        "strategies": counts, "nativeCompatibility": compatibility_counts,
        "nativeText": native_counts["text"], "nativeShape": native_counts["shape"],
        "nativeTable": native_counts["table"], "nativeChart": native_counts["chart"], "nativeImage": native_counts["image"],
        "nativeSvg": native_counts["svg"], "media": native_counts["media"],
        "partialSvgFallback": sum(decision["strategy"] == "svg" for decision in decisions),
        "imageFallback": sum(decision["strategy"] == "image" for decision in decisions), "fullSlideSvg": 0,
        "overflowCorrections": sum(correction["kind"] in {"overflow-frame-growth", "text-overflow"} for correction in corrections),
        "outOfBoundsCorrections": sum(correction["kind"] == "bounds" for correction in corrections),
        "overlapCorrections": sum(correction["kind"] == "overlap" for correction in corrections),
        "otherCorrections": sum(correction["kind"] not in {"overflow-frame-growth", "text-overflow", "bounds", "overlap"} for correction in corrections),
        "unresolvedWarnings": len(diagnostics), "corrections": len(corrections), "diagnostics": len(diagnostics),
        "embeddedLocalAssets": len(asset_resolutions),
        "unresolvedLocalResourceReferences": len(resource_scan["unresolved"]),
    }
    report = {
        "format": "bento/html-conversion-report/v1",
        "sourceOfTruth": ["chapter HTML", "registry JSON"],
        "canvas": {"width": CANVAS_WIDTH, "height": CANVAS_HEIGHT},
        "browser": layout.browser,
        "chapters": [{"chapterId": chapter.chapter_id, "html": chapter.html_path.name, "registry": chapter.registry_path.name} for chapter in chapters],
        "summary": summary,
        "elements": decisions,
        "corrections": corrections,
        "diagnostics": diagnostics,
        "assetResolution": asset_resolutions,
        "resourceScan": resource_scan,
        "registryCoverage": {key: len(registry.get(key, {})) for key in ("assets", "fonts", "equations", "figures", "tables", "charts")},
        "runtimeIntegrity": None,
        "browserCheck": None,
        "visualComparison": None,
    }
    return HtmlConversionResult(document, report)
