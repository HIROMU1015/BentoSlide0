"""End-to-end HTML-first build orchestration and evidence bundle creation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .bento_validator import validate_bento_doc
from .browser_check import BrowserCheckReport, run_browser_check
from .errors import BentoConverterError
from .html_converter import HtmlConversionResult, convert_html_layout
from .html_document import assert_runtime_integrity, extract_bento_doc, load_html, write_embedded_document
from .html_layout import LayoutResult, extract_computed_layout
from .html_source import discover_chapters, discover_source_unit, merge_registries
from .visual_comparison import compare_crops, compare_images


CRITICAL_ROLES = {"title", "main-claim", "primary-visual", "conclusion"}
CRITICAL_SOURCE_TYPES = {"equation", "table", "chart", "image", "svg"}


def _critical_reason(decision: dict[str, Any]) -> str | None:
    if decision.get("critical"):
        return "data-bento-critical=true"
    if decision.get("role") in CRITICAL_ROLES:
        return f"role={decision['role']}"
    if decision.get("sourceTag") in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        return f"sourceTag={decision['sourceTag']}"
    if decision.get("sourceType") in CRITICAL_SOURCE_TYPES:
        return f"sourceType={decision['sourceType']}"
    return None


@dataclass(frozen=True)
class HtmlBuildResult:
    html_path: Path
    json_path: Path
    report_path: Path
    source_screenshots: tuple[str, ...]
    bento_screenshots: tuple[str, ...]
    document: dict[str, Any]
    report: dict[str, Any]


def _json_path(html_path: Path) -> Path:
    name = html_path.name
    return html_path.with_name(name[: -len(".html")] + ".json") if name.endswith(".bento.html") else html_path.with_suffix(".bento.json")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _clear_generated_files(*directories: Path) -> None:
    for directory in directories:
        if not directory.is_dir():
            continue
        for pattern in ("*.png", "*.json"):
            for path in directory.glob(pattern):
                if path.is_file():
                    path.unlink()


def _semantic_comparison(layout: LayoutResult, conversion: HtmlConversionResult, screenshots: tuple[str, ...], root: Path) -> dict[str, Any]:
    source_ids = [slide["id"] for slide in layout.slides]
    output_ids = [slide["id"] for slide in conversion.document["slides"]]
    decisions = conversion.report["elements"]
    emitted = [decision for decision in decisions if decision["strategy"] != "ignore"]
    output_slides = {slide["id"]: slide for slide in conversion.document["slides"]}
    pairs = []
    for index, source_path in enumerate(layout.source_screenshots):
        slide_id = source_ids[index]
        output_slide = output_slides.get(slide_id, {"elements": []})
        slide_decisions = [decision for decision in decisions if decision["slideId"] == slide_id]
        expected_ids = [emitted_id for decision in slide_decisions if decision["strategy"] != "ignore" for emitted_id in decision.get("emittedIds", [])]
        actual_ids = [element["id"] for element in output_slide["elements"]]
        bento_path = screenshots[index] if index < len(screenshots) else None
        checks = {
            "allEmittedElementsPresent": expected_ids == actual_ids,
            "contentPreserved": all(decision.get("contentPreserved", True) for decision in slide_decisions),
            "majorPlacementTracked": all("bentoFrame" in decision for decision in slide_decisions if decision["strategy"] != "ignore"),
            "hierarchyAndZOrderPreserved": expected_ids == actual_ids,
            "stylesTracked": all("styleChecks" in decision for decision in slide_decisions if decision["strategy"] != "ignore"),
            "insideCanvas": all(element["x"] >= 0 and element["y"] >= 0 and element["x"] + element["w"] <= 1280 and element["y"] + element["h"] <= 720 for element in output_slide["elements"]),
        }
        image_comparison = compare_images(source_path, bento_path) if bento_path else {
            "status": "fail", "warnings": ["Bento screenshot is missing"]
        }
        element_comparisons = []
        for decision in slide_decisions:
            source_type = decision.get("sourceType")
            role = decision.get("role")
            critical_reason = _critical_reason(decision)
            important = critical_reason is not None or decision.get("compareCrop") or "title" in decision["elementId"].lower()
            if not important or not bento_path or "sourceFrame" not in decision or "bentoFrame" not in decision:
                continue
            crop = compare_crops(
                source_path,
                bento_path,
                decision.get("sourceBoundingFrame", decision["sourceFrame"]),
                decision.get("bentoBoundingFrame", decision["bentoFrame"]),
            )
            if crop:
                contribution = "none"
                if crop["status"] == "fail":
                    contribution = "slide-fail" if critical_reason else "slide-warning"
                elif crop["status"] == "warning":
                    contribution = "slide-warning"
                element_comparisons.append({
                    "elementId": decision["elementId"], "role": role, "sourceType": source_type,
                    "critical": critical_reason is not None, "criticalReason": critical_reason,
                    "statusContribution": contribution,
                    "imageComparison": crop,
                })
        semantic_ok = all(checks.values())
        critical_crop_fail = any(item["critical"] and item["imageComparison"]["status"] == "fail" for item in element_comparisons)
        status = "fail" if not semantic_ok or image_comparison["status"] == "fail" or critical_crop_fail else image_comparison["status"]
        if status == "pass" and any(item["imageComparison"]["status"] != "pass" for item in element_comparisons):
            status = "warning"
        pairs.append({
            "slideId": slide_id,
            "source": Path(source_path).resolve().relative_to(root).as_posix(),
            "bento": Path(bento_path).resolve().relative_to(root).as_posix() if bento_path else None,
            "status": status,
            "checks": checks,
            "imageComparison": image_comparison,
            "elementComparisons": element_comparisons,
        })
    pair_checks_pass = all(all(pair["checks"].values()) for pair in pairs)
    return {
        "method": "semantic structure plus paired Chromium screenshots; pixel identity is not required",
        "passed": source_ids == output_ids and len(pairs) == len(source_ids) and all(pair["bento"] for pair in pairs) and pair_checks_pass and not any(pair["status"] == "fail" for pair in pairs),
        "slideOrderMatches": source_ids == output_ids,
        "sourceElementCandidates": len(decisions),
        "emittedSourceElements": len(emitted),
        "emittedBentoElements": sum(len(slide["elements"]) for slide in conversion.document["slides"]),
        "ignoredElements": len(decisions) - len(emitted),
        "coordinateBasis": "Both source and Bento frames are normalized to the 1280x720 canvas.",
        "pairs": pairs,
    }


def build_from_html(
    *,
    html_dir: str | Path | None = None,
    registry_dir: str | Path | None = None,
    html_path: str | Path | None = None,
    registry_path: str | Path | None = None,
    base_path: str | Path,
    output_path: str | Path,
    browser_executable: str | Path | None = None,
    browser_check: bool = True,
) -> HtmlBuildResult:
    """Build, validate, render, round-trip, and report an HTML-first deck."""

    output = Path(output_path).resolve()
    base = Path(base_path).resolve()
    if output == base:
        raise BentoConverterError("Output path must differ from the Bento base HTML.")
    explicit = html_path is not None or registry_path is not None
    modular = html_dir is not None or registry_dir is not None
    if explicit == modular:
        raise BentoConverterError("Choose exactly one source form: --html/--registry or --html-dir/--registry-dir.")
    if explicit:
        if html_path is None or registry_path is None:
            raise BentoConverterError("Single-file conversion requires both HTML and registry paths.")
        chapters = [discover_source_unit(html_path, registry_path)]
    else:
        if html_dir is None or registry_dir is None:
            raise BentoConverterError("Modular conversion requires both HTML and registry directories.")
        chapters = discover_chapters(html_dir, registry_dir)
    root = output.parent
    source_dir = root / "screenshots" / "source"
    bento_dir = root / "screenshots" / "bento"
    diagnostics_dir = root / "diagnostics"
    diagnostics_dir.mkdir(parents=True, exist_ok=True)
    _clear_generated_files(source_dir, bento_dir, diagnostics_dir)

    registry = merge_registries(chapters)
    layout = extract_computed_layout(chapters, source_dir, browser_executable=browser_executable)
    conversion = convert_html_layout(layout, registry, chapters)
    validate_bento_doc(conversion.document)

    write_embedded_document(base, output, conversion.document)
    base_html = load_html(base)
    output_html = load_html(output)
    assert_runtime_integrity(base_html, output_html)
    if extract_bento_doc(output_html) != conversion.document:
        raise BentoConverterError("Generated #bento-doc does not round-trip exactly.")

    json_path = _json_path(output)
    _write_json(json_path, conversion.document)
    _write_json(diagnostics_dir / "computed-layout.json", {"slides": layout.slides})
    _write_json(diagnostics_dir / "merged-registry.json", registry)

    browser_report: BrowserCheckReport | None = None
    bento_screenshots: tuple[str, ...] = ()
    if browser_check:
        browser_report = run_browser_check(
            output,
            screenshots_dir=bento_dir,
            screenshot_prefix="bento-slide",
            browser_executable=browser_executable,
        )
        bento_screenshots = browser_report.screenshots
        _write_json(diagnostics_dir / "browser-check.json", browser_report.as_dict())

    report = dict(conversion.report)
    report["runtimeIntegrity"] = True
    report["browserCheck"] = browser_report.as_dict() if browser_report else {"skipped": True}
    report["visualComparison"] = _semantic_comparison(layout, conversion, bento_screenshots, root) if browser_report else {"skipped": True}
    if browser_report:
        visual_pairs = report["visualComparison"]["pairs"]
        report["summary"].update({
            "visualPassSlides": sum(pair["status"] == "pass" for pair in visual_pairs),
            "visualWarningSlides": sum(pair["status"] == "warning" for pair in visual_pairs),
            "visualFailSlides": sum(pair["status"] == "fail" for pair in visual_pairs),
            "maxVisualDifference": max((pair["imageComparison"].get("normalizedPixelDifference", 1) for pair in visual_pairs), default=0),
            "averageVisualDifference": round(sum(pair["imageComparison"].get("normalizedPixelDifference", 1) for pair in visual_pairs) / max(len(visual_pairs), 1), 6),
            "criticalElementPass": sum(item["imageComparison"]["status"] == "pass" for pair in visual_pairs for item in pair["elementComparisons"] if item["critical"]),
            "criticalElementWarning": sum(item["imageComparison"]["status"] == "warning" for pair in visual_pairs for item in pair["elementComparisons"] if item["critical"]),
            "criticalElementFail": sum(item["imageComparison"]["status"] == "fail" for pair in visual_pairs for item in pair["elementComparisons"] if item["critical"]),
        })
    report_path = root / "conversion-report.json"
    _write_json(report_path, report)
    resource_scan = report.get("resourceScan", {"passed": True, "unresolved": []})
    _write_json(diagnostics_dir / "resource-scan.json", resource_scan)
    if browser_report and not report["visualComparison"]["passed"]:
        failed = [pair["slideId"] for pair in report["visualComparison"]["pairs"] if pair["status"] == "fail"]
        raise BentoConverterError(f"Source/Bento visual comparison failed for slides: {failed}")
    if not resource_scan["passed"]:
        raise BentoConverterError(f"Unresolved local resource references: {resource_scan['unresolved']}")
    return HtmlBuildResult(output, json_path, report_path, layout.source_screenshots, bento_screenshots, conversion.document, report)
