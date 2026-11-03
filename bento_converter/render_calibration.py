"""Bounded, renderer-aware calibration for editable equations and inline-math text."""

from __future__ import annotations

import math
import re
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


INLINE_MATH = re.compile(r"(?<!\$)\$[^$\n]+\$(?!\$)")
INLINE_MIN_SCALE = 0.85
# Fonts that only just fit may wrap again in another engine or font build; keep 2% spare.
INLINE_SAFETY = 0.98


def _inline_math_targets(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Plain text elements whose content carries inline `$...$` math."""
    return [
        {"slideIndex": slide_index, "slideId": slide["id"], "elementId": element["id"], "element": element}
        for slide_index, slide in enumerate(document["slides"])
        for element in slide["elements"]
        if element.get("type") == "text"
        and not _is_equation(element)
        and isinstance(element.get("html"), str)
        and INLINE_MATH.search(element["html"])
    ]


def _fit_inline_math_text(
    html_path: Path, targets: list[dict[str, Any]], harness: BrowserHarness,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Find, in the Bento renderer, the font scale at which each text keeps its line count.

    Bento renders inline math as MathML, which is usually wider than the source's
    Unicode sub/sup. A text that fitted its box in the source may therefore gain a
    line. The search runs in the page, so one render covers every candidate scale.
    """
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
                """(slide, [wanted, minScale]) => {
                  const result = {};
                  for (const host of slide.querySelectorAll('[data-el-id]')) {
                    const id = host.dataset.elId;
                    if (!wanted.includes(id)) continue;
                    const inner = host.querySelector('.bento-text-inner');
                    if (!inner) continue;
                    const style = getComputedStyle(inner);
                    const fontSize = parseFloat(style.fontSize);
                    const lineHeight = style.lineHeight === 'normal'
                      ? fontSize * 1.2 : parseFloat(style.lineHeight);
                    const box = host.offsetHeight;
                    // Inline MathML may raise a line box a little; only a whole extra line counts.
                    const fits = () => inner.offsetHeight <= box + lineHeight * 0.45;
                    const rendered = inner.offsetHeight;
                    if (fits()) { result[id] = {box, rendered, lineHeight, scale: 1}; continue; }
                    const original = inner.style.fontSize;
                    const at = scale => { inner.style.fontSize = `${fontSize * scale}px`; return fits(); };
                    let scale = null;
                    if (at(minScale)) {
                      let low = minScale, high = 1;
                      for (let step = 0; step < 8; step += 1) {
                        const middle = (low + high) / 2;
                        if (at(middle)) low = middle; else high = middle;
                      }
                      scale = Math.floor(low * 1000) / 1000;
                    }
                    inner.style.fontSize = original;
                    result[id] = {box, rendered, lineHeight, scale};
                  }
                  return result;
                }""",
                [sorted(wanted_by_slide[slide_index]), INLINE_MIN_SCALE],
            )
            for element_id, metric in measured.items():
                result[(slide_id, element_id)] = metric
    return result


def _apply_inline_math_fit(
    targets: list[dict[str, Any]], measured: dict[tuple[str, str], dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    corrections: list[dict[str, Any]] = []
    measurements: list[dict[str, Any]] = []
    for target in targets:
        key = (target["slideId"], target["elementId"])
        metric = measured.get(key)
        if not metric:
            measurements.append({"slideId": key[0], "elementId": key[1], "status": "unmeasured"})
            continue
        scale = metric.get("scale")
        status = "fits" if scale == 1 else "unresolved" if scale is None else "scaled"
        measurements.append({"slideId": key[0], "elementId": key[1], "status": status, **metric})
        if status != "scaled":
            continue
        scale = max(INLINE_MIN_SCALE, scale * INLINE_SAFETY)
        element = target["element"]
        before = {"fontSize": element.get("fontSize")}
        element["fontSize"] = _compact(math.floor(float(element.get("fontSize") or 16) * scale * 100) / 100)
        corrections.append({
            "slideId": key[0], "elementId": key[1], "kind": "inline-math-text-fit",
            "before": before, "after": {"fontSize": element["fontSize"]},
            "measurement": {
                "boxHeight": _compact(metric["box"]), "renderedHeight": _compact(metric["rendered"]),
                "lineHeight": _compact(metric["lineHeight"]), "fontScale": _compact(scale),
            },
            "contentChanged": False,
        })
    return corrections, measurements


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
    position may be adjusted within conservative bounds. Text with inline math
    that gains a line in Bento gets a bounded font-size reduction instead.
    """

    source_metrics = _source_equations(layout)
    targets = _equation_targets(document, report, source_metrics)
    inline_targets = _inline_math_targets(document)
    if not targets and not inline_targets:
        return {
            "format": "bento/render-calibration/v1", "equations": 0, "adjusted": 0,
            "inlineMathTexts": 0, "inlineMathAdjusted": 0, "measurements": [],
        }

    corrections: list[dict[str, Any]] = []
    measurements: list[dict[str, Any]] = []
    inline_corrections: list[dict[str, Any]] = []
    inline_measurements: list[dict[str, Any]] = []
    base = Path(base_path).resolve()
    parent = Path(temporary_parent).resolve()
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".equation-calibration-", dir=parent) as temporary_name:
        temporary = Path(temporary_name)
        if targets:
            _calibrate_equations(base, temporary, document, targets, harness, corrections, measurements)
        if inline_targets:
            # Measured after the equation pass, against the document as it will be written.
            third_path = _write_probe(base, temporary, document, "pass-3.bento.html")
            inline_corrections, inline_measurements = _apply_inline_math_fit(
                inline_targets, _fit_inline_math_text(third_path, inline_targets, harness),
            )
    decisions = {
        (decision["slideId"], emitted_id): decision
        for decision in report.get("elements", ())
        for emitted_id in decision.get("emittedIds", ())
    }
    for correction in inline_corrections:
        decision = decisions.get((correction["slideId"], correction["elementId"]))
        if decision is not None:
            decision.setdefault("adjustments", []).append(
                f"inline-math-text-fit: {correction['before']} -> {correction['after']}"
            )

    all_corrections = corrections + inline_corrections
    report.setdefault("corrections", []).extend(all_corrections)
    summary = report.setdefault("summary", {})
    summary["equationRendererCalibrations"] = len(corrections)
    summary["inlineMathTextFits"] = len(inline_corrections)
    summary["corrections"] = int(summary.get("corrections") or 0) + len(all_corrections)
    summary["otherCorrections"] = int(summary.get("otherCorrections") or 0) + len(all_corrections)
    return {
        "format": "bento/render-calibration/v1",
        "equations": len(targets),
        "adjusted": len(corrections),
        "inlineMathTexts": len(inline_targets),
        "inlineMathAdjusted": len(inline_corrections),
        "measurements": measurements,
        "inlineMeasurements": inline_measurements,
        "corrections": all_corrections,
    }


def _calibrate_equations(
    base: Path, temporary: Path, document: dict[str, Any], targets: list[dict[str, Any]],
    harness: BrowserHarness, corrections: list[dict[str, Any]], measurements: list[dict[str, Any]],
) -> None:
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

