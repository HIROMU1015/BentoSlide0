"""Bounded, renderer-aware calibration for editable standalone equations."""

from __future__ import annotations

import math
import tempfile
from pathlib import Path
from typing import Any

from .browser_harness import BrowserHarness
from .html_document import write_embedded_document
from .html_layout import CANVAS_HEIGHT, CANVAS_WIDTH, LayoutResult


def _compact(value: float) -> float | int:
    rounded = round(float(value), 3)
    return int(rounded) if rounded.is_integer() else rounded


def _is_equation(element: dict[str, Any]) -> bool:
    source = element.get("html")
    return (
        element.get("type") == "text"
        and isinstance(source, str)
        and source.startswith("$$")
        and source.endswith("$$")
    )


def _source_equations(layout: LayoutResult) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (slide["id"], element["id"]): element["equationMetrics"]
        for slide in layout.slides
        for element in slide.get("elements", ())
        if element.get("type") == "equation" and element.get("equationMetrics")
    }


def _equation_targets(
    document: dict[str, Any], report: dict[str, Any], source_metrics: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    decisions = {
        (decision["slideId"], emitted_id): decision
        for decision in report.get("elements", ())
        for emitted_id in decision.get("emittedIds", ())
    }
    targets: list[dict[str, Any]] = []
    for slide_index, slide in enumerate(document["slides"]):
        for element in slide["elements"]:
            if not _is_equation(element):
                continue
            decision = decisions.get((slide["id"], element["id"]))
            if not decision:
                continue
            metrics = source_metrics.get((slide["id"], decision["elementId"]))
            if not metrics:
                continue
            targets.append({
                "slideIndex": slide_index,
                "slideId": slide["id"],
                "elementId": element["id"],
                "sourceElementId": decision["elementId"],
                "element": element,
                "source": metrics,
                "decision": decision,
            })
    return targets


def _measure(html_path: Path, targets: list[dict[str, Any]], harness: BrowserHarness) -> dict[tuple[str, str], dict[str, Any]]:
    wanted_by_slide: dict[int, set[str]] = {}
    slide_ids: dict[int, str] = {}
    for target in targets:
        wanted_by_slide.setdefault(target["slideIndex"], set()).add(target["elementId"])
        slide_ids[target["slideIndex"]] = target["slideId"]
    result: dict[tuple[str, str], dict[str, Any]] = {}
    with harness.page("bentoCheck") as page:
        page.goto(html_path.as_uri(), wait_until="load")
        page.wait_for_function("window.bento && window.bento.doc && typeof window.bento.serialize === 'function'")
        harness.settle(page)
        for slide_index in sorted(wanted_by_slide):
            slide_id = slide_ids[slide_index]
            page.locator(".ed-thumb").nth(slide_index).click()
            page.wait_for_function(
                "expected => document.querySelector('.ed-stage .bento-slide')?.dataset.slideId === expected",
                arg=slide_id,
            )
            harness.settle(page)
            measured = page.locator(".ed-stage .bento-slide").evaluate(
                """(slide, wanted) => {
                  const sr = slide.getBoundingClientRect();
                  const sx = 1280 / sr.width;
                  const sy = 720 / sr.height;
                  const result = {};
                  for (const host of slide.querySelectorAll('[data-el-id]')) {
                    const id = host.dataset.elId;
                    if (!wanted.includes(id)) continue;
                    const maths = [...host.querySelectorAll('math')];
                    const glyphRoots = maths.flatMap(node => {
                      const children = [...node.children].filter(child => ![
                        'annotation', 'annotation-xml'
                      ].includes(child.tagName.toLowerCase()));
                      return children.length ? children : [node];
                    });
                    const rects = glyphRoots.map(node => node.getBoundingClientRect())
                      .filter(rect => rect.width > 0 && rect.height > 0);
                    if (!rects.length) continue;
                    const left = Math.min(...rects.map(rect => rect.left));
                    const top = Math.min(...rects.map(rect => rect.top));
                    const right = Math.max(...rects.map(rect => rect.right));
                    const bottom = Math.max(...rects.map(rect => rect.bottom));
                    const hr = host.getBoundingClientRect();
                    result[id] = {
                      glyphFrame: {x:(left-sr.left)*sx,y:(top-sr.top)*sy,w:(right-left)*sx,h:(bottom-top)*sy},
                      elementFrame: {x:(hr.left-sr.left)*sx,y:(hr.top-sr.top)*sy,w:hr.width*sx,h:hr.height*sy},
                    };
                  }
                  return result;
                }""",
                sorted(wanted_by_slide[slide_index]),
            )
            for element_id, metric in measured.items():
                result[(slide_id, element_id)] = metric
    return result


def _write_probe(base_path: Path, directory: Path, document: dict[str, Any], name: str) -> Path:
    target = directory / name
    write_embedded_document(base_path, target, document)
    return target


def calibrate_equation_rendering(
    *,
    base_path: str | Path,
    document: dict[str, Any],
    report: dict[str, Any],
    layout: LayoutResult,
    harness: BrowserHarness,
    temporary_parent: str | Path,
) -> dict[str, Any]:
    """Measure Bento MathML and apply small native text-only geometry corrections.

    The source HTML is never changed. The LaTeX, IDs, and decoration elements are
    also left intact; only the editable equation text element's font size and
    position may be adjusted within conservative bounds.
    """

    source_metrics = _source_equations(layout)
    targets = _equation_targets(document, report, source_metrics)
    if not targets:
        return {"format": "bento/render-calibration/v1", "equations": 0, "adjusted": 0, "measurements": []}

    corrections: list[dict[str, Any]] = []
    measurements: list[dict[str, Any]] = []
    base = Path(base_path).resolve()
    parent = Path(temporary_parent).resolve()
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".equation-calibration-", dir=parent) as temporary_name:
        temporary = Path(temporary_name)
        first_path = _write_probe(base, temporary, document, "pass-1.bento.html")
        first = _measure(first_path, targets, harness)

        scaled_targets: list[dict[str, Any]] = []
        for target in targets:
            key = (target["slideId"], target["elementId"])
            rendered = first.get(key)
            source = target["source"].get("glyphFrame")
            if not rendered or not source:
                measurements.append({"slideId": key[0], "elementId": key[1], "status": "unmeasured"})
                continue
            glyph = rendered["glyphFrame"]
            if min(source["w"], source["h"], glyph["w"], glyph["h"]) <= 0:
                continue
            width_ratio = source["w"] / glyph["w"]
            height_ratio = source["h"] / glyph["h"]
            scale = max(0.8, min(1.25, math.sqrt(width_ratio * height_ratio)))
            element = target["element"]
            before = {field: element[field] for field in ("x", "y", "w", "h", "fontSize")}
            if abs(scale - 1.0) >= 0.015:
                element["fontSize"] = _compact(max(1.0, float(element.get("fontSize") or 16) * scale))
                scaled_targets.append(target)
            measurements.append({
                "slideId": key[0], "elementId": key[1], "status": "measured",
                "sourceRenderer": target["source"].get("renderer"),
                "sourceGlyphFrame": source, "initialBentoGlyphFrame": glyph,
                "widthRatio": _compact(width_ratio), "heightRatio": _compact(height_ratio),
                "fontScale": _compact(scale), "before": before,
            })

        second_path = _write_probe(base, temporary, document, "pass-2.bento.html")
        second = _measure(second_path, targets, harness)
        for target in targets:
            key = (target["slideId"], target["elementId"])
            rendered = second.get(key)
            source = target["source"].get("glyphFrame")
            if not rendered or not source:
                continue
            glyph = rendered["glyphFrame"]
            source_center = (source["x"] + source["w"] / 2, source["y"] + source["h"] / 2)
            target_center = (glyph["x"] + glyph["w"] / 2, glyph["y"] + glyph["h"] / 2)
            dx = max(-12.0, min(12.0, source_center[0] - target_center[0]))
            dy = max(-12.0, min(12.0, source_center[1] - target_center[1]))
            element = target["element"]
            before = {field: element[field] for field in ("x", "y", "w", "h", "fontSize")}
            if abs(dx) >= 0.25:
                element["x"] = _compact(max(0.0, min(CANVAS_WIDTH - float(element["w"]), float(element["x"]) + dx)))
            if abs(dy) >= 0.25:
                element["y"] = _compact(max(0.0, min(CANVAS_HEIGHT - float(element["h"]), float(element["y"]) + dy)))
            after = {field: element[field] for field in ("x", "y", "w", "h", "fontSize")}
            if after != before or target in scaled_targets:
                original = next(
                    (item["before"] for item in measurements if item.get("slideId") == key[0] and item.get("elementId") == key[1] and "before" in item),
                    before,
                )
                correction = {
                    "slideId": key[0], "elementId": key[1], "kind": "equation-renderer-calibration",
                    "before": original, "after": after,
                    "measurement": {
                        "sourceGlyphFrame": source,
                        "finalMeasuredGlyphFrame": glyph,
                        "translation": {"x": _compact(dx), "y": _compact(dy)},
                    },
                    "contentChanged": False,
                }
                corrections.append(correction)
                target["decision"].setdefault("adjustments", []).append(
                    f"equation-renderer-calibration: {original} -> {after}"
                )
                target["decision"]["bentoFrame"] = {
                    field: element[field] for field in ("x", "y", "w", "h")
                }

    report.setdefault("corrections", []).extend(corrections)
    summary = report.setdefault("summary", {})
    summary["equationRendererCalibrations"] = len(corrections)
    summary["corrections"] = int(summary.get("corrections") or 0) + len(corrections)
    summary["otherCorrections"] = int(summary.get("otherCorrections") or 0) + len(corrections)
    return {
        "format": "bento/render-calibration/v1",
        "equations": len(targets),
        "adjusted": len(corrections),
        "measurements": measurements,
        "corrections": corrections,
    }
