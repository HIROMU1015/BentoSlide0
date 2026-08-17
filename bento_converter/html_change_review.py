"""Deterministic browser evidence for an applied whole-deck HTML change."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .browser_harness import BrowserHarness
from .errors import BrowserCheckError
from .html_layout import extract_computed_layout
from .html_source import discover_source_unit


POST_APPLY_REVIEW_FORMAT = "bento/html-change-post-apply-review/v1"
POST_APPLY_REPORT_FORMAT = "bento/html-change-post-apply-browser-report/v1"
CANVAS_WIDTH = 1280.0
CANVAS_HEIGHT = 720.0
TOLERANCE = 1.0


@dataclass(frozen=True)
class HtmlChangeBrowserEvidence:
    report: dict[str, Any]
    environment: dict[str, Any]
    screenshots: dict[str, Path]


def _frame_issue(element: dict[str, Any]) -> dict[str, Any] | None:
    geometry_frame = {field: float(element.get(field) or 0) for field in ("x", "y", "w", "h")}
    visible = element.get("boundingFrame")
    source = visible if isinstance(visible, dict) else geometry_frame
    frame = {field: float(source.get(field) or 0) for field in ("x", "y", "w", "h")}
    if (
        not all(math.isfinite(value) for value in frame.values())
        or frame["w"] < 0
        or frame["h"] < 0
        or frame["x"] < -TOLERANCE
        or frame["y"] < -TOLERANCE
        or frame["x"] + frame["w"] > CANVAS_WIDTH + TOLERANCE
        or frame["y"] + frame["h"] > CANVAS_HEIGHT + TOLERANCE
    ):
        return {
            "kind": "element-out-of-bounds",
            "elementId": element.get("id"),
            "frame": frame,
            "geometryFrame": geometry_frame,
        }
    return None


def _overflow_issue(element: dict[str, Any]) -> dict[str, Any] | None:
    semantic_type = element.get("type") in {"text", "equation", "table", "chart"}
    contains_text = bool(str(element.get("text") or "").strip())
    if not semantic_type and not contains_text:
        return None
    client_width = float(element.get("clientWidth") or element.get("w") or 0)
    client_height = float(element.get("clientHeight") or element.get("h") or 0)
    scroll_width = float(element.get("scrollWidth") or client_width)
    scroll_height = float(element.get("scrollHeight") or client_height)
    if scroll_width > client_width + TOLERANCE or scroll_height > client_height + TOLERANCE:
        return {
            "kind": "element-content-overflow",
            "elementId": element.get("id"),
            "client": {"w": client_width, "h": client_height},
            "scroll": {"w": scroll_width, "h": scroll_height},
        }
    return None


def collect_html_change_browser_evidence(
    *,
    html_path: str | Path,
    registry_path: str | Path,
    affected_slide_ids: Iterable[str],
    screenshots_dir: str | Path,
    browser_executable: str | Path | None = None,
) -> HtmlChangeBrowserEvidence:
    """Render the current deck and return strict evidence for every affected slide."""

    affected = tuple(dict.fromkeys(str(value) for value in affected_slide_ids))
    if not affected:
        raise BrowserCheckError("Post-apply HTML review requires affected slides")
    source = discover_source_unit(html_path, registry_path)
    with BrowserHarness(browser_executable) as harness:
        layout = extract_computed_layout(
            [source], screenshots_dir, harness=harness,
        )
        harness.assert_no_blocked_network("sourceLayout")
        environment = harness.report()

    slides = {str(slide["id"]): slide for slide in layout.slides}
    missing = [slide_id for slide_id in affected if slide_id not in slides]
    if missing:
        raise BrowserCheckError(f"Affected slides are absent from the rendered deck: {missing}")
    screenshot_by_slide = {
        str(slide["id"]): Path(path)
        for slide, path in zip(layout.slides, layout.source_screenshots, strict=True)
    }
    checks: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    selected_screenshots: dict[str, Path] = {}
    for slide_id in affected:
        slide = slides[slide_id]
        issues = [
            issue
            for element in slide.get("elements", [])
            for issue in (_frame_issue(element), _overflow_issue(element))
            if issue is not None
        ]
        screenshot = screenshot_by_slide[slide_id]
        if not screenshot.is_file():
            issues.append({"kind": "missing-screenshot"})
        else:
            selected_screenshots[slide_id] = screenshot
        check = {
            "slideId": slide_id,
            "sourceSize": {"w": slide.get("sourceWidth"), "h": slide.get("sourceHeight")},
            "elementCount": len(slide.get("elements", [])),
            "issues": issues,
            "status": "pass" if not issues else "fail",
        }
        checks.append(check)
        if issues:
            failures.append(check)
    if failures:
        summary = ", ".join(
            f"{item['slideId']} ({', '.join(issue['kind'] for issue in item['issues'])})"
            for item in failures
        )
        raise BrowserCheckError("Post-apply HTML browser review failed: " + summary)
    return HtmlChangeBrowserEvidence(
        report={
            "format": POST_APPLY_REPORT_FORMAT,
            "status": "pass",
            "affectedSlideIds": list(affected),
            "checks": checks,
            "networkPolicy": environment["browserEnvironment"]["networkPolicy"],
            "renderPolicy": environment["browserEnvironment"]["renderPolicy"],
        },
        environment=environment,
        screenshots=selected_screenshots,
    )
