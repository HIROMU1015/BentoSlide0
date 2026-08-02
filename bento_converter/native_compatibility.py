"""Classify whether computed HTML/CSS can be represented by Bento native fields."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Compatibility:
    classification: str
    reasons: tuple[str, ...] = ()
    adjustments: tuple[str, ...] = ()


def _active(value: object, inactive: set[str] | None = None) -> bool:
    normalized = str(value or "").strip().lower()
    return normalized not in (inactive or {"", "none", "normal", "auto"})


def _background_layers(value: object) -> int:
    text = str(value or "").strip()
    if not text or text == "none":
        return 0
    depth = 0
    layers = 1
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            layers += 1
    return layers


def classify_native_compatibility(element: dict[str, Any]) -> Compatibility:
    style = element.get("style", {})
    element_type = element.get("type")
    tag = element.get("tag")
    transform = element.get("transform", {})
    if tag == "canvas" or element_type == "canvas":
        return Compatibility("image-required", ("Canvas/WebGL output has no editable DOM representation.",))
    markup = str(element.get("outerHTML") or element.get("svg") or "").lower()
    if element_type == "svg" and "<foreignobject" in markup and any(token in markup for token in ("<canvas", "<iframe", "<object", "<embed", "<video")):
        return Compatibility("image-required", ("SVG foreignObject contains rendered/embedded content that cannot be preserved safely.",))
    table = element.get("table")
    if element_type == "table" and isinstance(table, dict) and not table.get("simpleTable", True):
        reasons = table.get("complexityReasons") or ["HTML table structure is not rectangular."]
        return Compatibility("localized-svg-recommended", tuple(str(reason) for reason in reasons))
    if transform.get("is3D") or transform.get("hasSkew"):
        return Compatibility("localized-svg-recommended", ("Skew or 3D transform cannot be represented by Bento rotation/scale.",))
    unsupported: list[str] = []
    if _active(style.get("clipPath")):
        unsupported.append("complex clip-path")
    if _active(style.get("maskImage")):
        unsupported.append("CSS mask")
    if _active(style.get("filter")):
        unsupported.append("CSS filter")
    if _active(style.get("backdropFilter")):
        unsupported.append("backdrop-filter")
    if _active(style.get("mixBlendMode"), {"", "normal"}):
        unsupported.append("mix-blend-mode")
    if element.get("pseudoElementDependent"):
        unsupported.append("visible pseudo-element dependency")
    if style.get("writingMode") not in {None, "", "horizontal-tb"}:
        unsupported.append("non-horizontal writing mode")
    background = style.get("backgroundImage")
    layers = _background_layers(background)
    if layers > 1:
        unsupported.append("multiple background layers")
    elif layers == 1 and "linear-gradient(" not in str(background) and element_type not in {"image", "svg"}:
        unsupported.append("CSS background image")
    if unsupported:
        return Compatibility("localized-svg-recommended", tuple(f"Bento native fields do not preserve {item}." for item in unsupported))

    adjustments: list[str] = []
    if style.get("display") in {"flex", "inline-flex"}:
        adjustments.append("Map flex alignment to Bento text align/valign.")
    if any(float(style.get(field) or 0) for field in ("paddingLeft", "paddingRight", "paddingTop", "paddingBottom")):
        adjustments.append("Inset the editable text frame by computed padding.")
    if abs(float(style.get("letterSpacing") or 0)) > 0.01:
        adjustments.append("Preserve computed letter spacing.")
    if layers == 1 and "linear-gradient(" in str(background):
        adjustments.append("Map simple linear gradient to Bento gradient stops.")
    if element_type == "image" and style.get("objectFit") in {"cover", "contain", "fill"}:
        adjustments.append("Map object-fit to Bento image fit.")
    if transform.get("hasTransform"):
        adjustments.append("Absorb translate/scale into the frame and preserve rotation around the visual center.")
    if adjustments:
        return Compatibility("native-with-adjustment", (), tuple(adjustments))
    return Compatibility("native-safe")


def classify_slide_background(slide: dict[str, Any]) -> Compatibility:
    style = slide.get("backgroundStyle", {})
    image = str(style.get("backgroundImage") or "none")
    layers = _background_layers(image)
    if layers == 0:
        return Compatibility("native-safe")
    if layers == 1 and image.startswith("linear-gradient("):
        return Compatibility("native-with-adjustment", (), ("Emit a native gradient shape behind slide content.",))
    if layers == 1 and image.startswith("url(") and style.get("backgroundRepeat") == "no-repeat":
        return Compatibility("native-with-adjustment", (), ("Emit a dedicated background image element.",))
    return Compatibility("localized-svg-recommended", ("Slide background uses layers/effects unsupported by the Bento background string.",))
