"""Render HTML chapters in Chromium and extract deterministic computed layout."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .browser_check import find_browser_executable
from .errors import BrowserCheckError, ValidationError, issue
from .html_source import SourceChapter
from .native_compatibility import classify_native_compatibility

CANVAS_WIDTH = 1280
CANVAS_HEIGHT = 720

EXTRACT_LAYOUT_JS = r"""
() => {
  const px = value => {
    const parsed = Number.parseFloat(value);
    return Number.isFinite(parsed) ? parsed : 0;
  };
  const transformInfo = (el, cs, r, sr, sx, sy) => {
    const matrix = cs.transform === 'none' ? new DOMMatrixReadOnly() : new DOMMatrixReadOnly(cs.transform);
    const scaleX = Math.hypot(matrix.a, matrix.b) || 1;
    const determinant = matrix.a * matrix.d - matrix.b * matrix.c;
    const scaleY = Math.abs(determinant / scaleX) || 1;
    const dot = matrix.a * matrix.c + matrix.b * matrix.d;
    const hasSkew = matrix.is2D && Math.abs(dot) > 0.0001;
    const rotation = matrix.is2D ? Math.atan2(matrix.b, matrix.a) * 180 / Math.PI : 0;
    const baseWidth = Number.isFinite(el.offsetWidth) ? el.offsetWidth : r.width / scaleX;
    const baseHeight = Number.isFinite(el.offsetHeight) ? el.offsetHeight : r.height / scaleY;
    const width = baseWidth * scaleX * sx;
    const height = baseHeight * scaleY * sy;
    const centerX = (r.left - sr.left + r.width / 2) * sx;
    const centerY = (r.top - sr.top + r.height / 2) * sy;
    return {
      raw: cs.transform,
      origin: cs.transformOrigin,
      matrix: matrix.is2D ? [matrix.a, matrix.b, matrix.c, matrix.d, matrix.e, matrix.f] : [...matrix.toFloat64Array()],
      is2D: matrix.is2D,
      is3D: !matrix.is2D,
      hasTransform: cs.transform !== 'none',
      hasSkew,
      rotation: Math.round(rotation * 1000) / 1000,
      scaleX, scaleY, translateX: matrix.e * sx, translateY: matrix.f * sy,
      centerX, centerY,
      frame: {x: centerX - width / 2, y: centerY - height / 2, w: width, h: height},
      boundingFrame: {x:(r.left-sr.left)*sx,y:(r.top-sr.top)*sy,w:r.width*sx,h:r.height*sy},
    };
  };
  const slides = [...document.querySelectorAll('section.slide[data-slide-id]')];
  return slides.map((slide, slideIndex) => {
    const sr = slide.getBoundingClientRect();
    const sx = 1280 / sr.width;
    const sy = 720 / sr.height;
    const explicit = [...slide.querySelectorAll('[data-bento-id],[data-bento-type],[data-bento-export]')];
    const implicit = [...slide.querySelectorAll('h1,h2,h3,h4,h5,h6,p,span,li,table,img,svg,video,audio,canvas')]
      .filter(el => !el.closest('[data-bento-id],[data-bento-type],[data-bento-export]') && !el.parentElement?.closest('h1,h2,h3,h4,h5,h6,p,span,li,table'));
    const candidates = [...explicit, ...implicit];
    const elements = candidates.map((el, index) => {
      const cs = getComputedStyle(el);
      const r = el.getBoundingClientRect();
      const tx = transformInfo(el, cs, r, sr, sx, sy);
      const tag = el.tagName.toLowerCase();
      const explicitType = el.dataset.bentoType;
      let inferred = explicitType || 'text';
      if (!explicitType && tag === 'table') inferred = 'table';
      if (!explicitType && tag === 'img') inferred = 'image';
      if (!explicitType && tag === 'svg') inferred = 'svg';
      if (!explicitType && (tag === 'video' || tag === 'audio')) inferred = 'media';
      if (!explicitType && tag === 'canvas') inferred = 'canvas';
      const chartScript = el.querySelector?.('script[type="application/json"][data-bento-chart],script[type="application/json"][data-chart-option]');
      let chartOption = null;
      if (chartScript) {
        try { chartOption = JSON.parse(chartScript.textContent); } catch (_) {}
      }
      const tableRows = tag === 'table' || inferred === 'table' ? [...el.querySelectorAll(':scope > thead > tr,:scope > tbody > tr,:scope > tfoot > tr,:scope > tr')] : [];
      const table = tag === 'table' || inferred === 'table' ? {
        rows: tableRows.map((row, rowIndex) => [...row.cells].map((cell, columnIndex) => {
          const cellStyle = getComputedStyle(cell);
          const cellRect = cell.getBoundingClientRect();
          return {
            html: cell.innerHTML,
            align: cellStyle.textAlign,
            color: cellStyle.color,
            bg: cellStyle.backgroundColor,
            bold: Number.parseInt(cellStyle.fontWeight, 10) >= 600,
            rowSpan: cell.rowSpan,
            colSpan: cell.colSpan,
            rowIndex,
            columnIndex,
            rect: {x:(cellRect.left-r.left)*sx,y:(cellRect.top-r.top)*sy,w:cellRect.width*sx,h:cellRect.height*sy},
            style: {
              color: cellStyle.color,
              backgroundColor: cellStyle.backgroundColor,
              fontFamily: cellStyle.fontFamily,
              fontSize: px(cellStyle.fontSize),
              fontWeight: Number.parseInt(cellStyle.fontWeight, 10) || cellStyle.fontWeight,
              textAlign: cellStyle.textAlign,
              verticalAlign: cellStyle.verticalAlign,
              padding: cellStyle.padding,
              border: cellStyle.border,
            },
            hasComplexContent: Boolean(cell.querySelector('img,svg,canvas,video,audio,table,[data-bento-type="chart"],[data-bento-type="complex"]')),
          };
        })),
        columnWidths: [...(el.querySelector('tr')?.cells || [])].map(cell => cell.getBoundingClientRect().width),
        headerRows: el.querySelectorAll('thead tr').length,
        nestedTable: Boolean(el.querySelector('table')),
      } : null;
      if (table) {
        const effectiveCounts = table.rows.map(row => row.reduce((sum, cell) => sum + cell.colSpan, 0));
        table.complexityReasons = [];
        if (table.rows.some(row => row.some(cell => cell.rowSpan > 1))) table.complexityReasons.push('HTML table contains rowspan unsupported by Bento native table');
        if (table.rows.some(row => row.some(cell => cell.colSpan > 1))) table.complexityReasons.push('HTML table contains colspan unsupported by Bento native table');
        if (new Set(effectiveCounts).size > 1) table.complexityReasons.push('HTML table rows have inconsistent effective column counts');
        if (table.nestedTable) table.complexityReasons.push('HTML table contains a nested table');
        if (table.rows.some(row => row.some(cell => cell.hasComplexContent))) table.complexityReasons.push('HTML table cell contains image/chart/complex content');
        if (table.headerRows > 1) table.complexityReasons.push('HTML table contains a multilevel header');
        table.simpleTable = table.complexityReasons.length === 0;
      }
      const layout = el.closest('[data-layout]');
      const exportMode = el.dataset.bentoExport || 'auto';
      const before = getComputedStyle(el, '::before');
      const after = getComputedStyle(el, '::after');
      const pseudoElementDependent = [before, after].some(pseudo => pseudo.content && !['none','normal','""'].includes(pseudo.content) && (pseudo.display !== 'none') && (Number.parseFloat(pseudo.opacity) || 1) > 0);
      return {
        id: el.dataset.bentoId || `auto-${slideIndex + 1}-${index + 1}`,
        domIndex: index,
        tag,
        type: inferred,
        exportMode,
        explicitId: Boolean(el.dataset.bentoId),
        critical: el.dataset.bentoCritical === 'true',
        compareCrop: el.dataset.bentoCompare === 'true',
        role: el.dataset.bentoRole || null,
        paperSource: el.dataset.paperSource || null,
        registryId: el.dataset.registryId || null,
        equationId: el.dataset.equationId || null,
        assetId: el.dataset.assetId || null,
        figureId: el.dataset.figureId || null,
        chartId: el.dataset.chartId || null,
        tableId: el.dataset.tableId || null,
        morphId: el.dataset.morphId || null,
        link: el.dataset.link || el.dataset.bentoLink || null,
        shape: el.dataset.bentoShape || null,
        lineStart: el.dataset.lineStart || null,
        lineEnd: el.dataset.lineEnd || null,
        strokeStyle: el.dataset.strokeStyle || null,
        from: el.dataset.from || null,
        to: el.dataset.to || null,
        layout: layout?.dataset.layout || null,
        layoutGroup: layout?.dataset.layoutId || layout?.dataset.bentoId || null,
        x: tx.frame.x,
        y: tx.frame.y,
        w: tx.frame.w,
        h: tx.frame.h,
        boundingFrame: tx.boundingFrame,
        offsetWidth: (Number.isFinite(el.offsetWidth) ? el.offsetWidth : r.width / tx.scaleX) * sx,
        offsetHeight: (Number.isFinite(el.offsetHeight) ? el.offsetHeight : r.height / tx.scaleY) * sy,
        clientWidth: (Number.isFinite(el.clientWidth) ? el.clientWidth : r.width / tx.scaleX) * sx,
        clientHeight: (Number.isFinite(el.clientHeight) ? el.clientHeight : r.height / tx.scaleY) * sy,
        scrollWidth: (Number.isFinite(el.scrollWidth) ? el.scrollWidth : r.width / tx.scaleX) * tx.scaleX * sx,
        scrollHeight: (Number.isFinite(el.scrollHeight) ? el.scrollHeight : r.height / tx.scaleY) * tx.scaleY * sy,
        rotation: tx.rotation,
        transform: tx,
        opacity: Number.parseFloat(cs.opacity) || 1,
        z: Number.isFinite(Number.parseInt(el.dataset.bentoZ, 10)) ? Number.parseInt(el.dataset.bentoZ, 10) : (Number.parseInt(cs.zIndex, 10) || index),
        text: el.textContent.trim(),
        html: el.innerHTML,
        outerHTML: el.outerHTML,
        svg: tag === 'svg' ? el.outerHTML : el.querySelector?.(':scope > svg')?.outerHTML || null,
        src: el.currentSrc || el.src || null,
        poster: el.poster || null,
        mediaKind: tag === 'audio' ? 'audio' : 'video',
        controls: Boolean(el.controls),
        autoplay: Boolean(el.autoplay),
        loop: Boolean(el.loop),
        muted: Boolean(el.muted),
        chartOption,
        table,
        pseudoElementDependent,
        style: {
          color: cs.color,
          backgroundColor: cs.backgroundColor,
          backgroundImage: cs.backgroundImage,
          backgroundSize: cs.backgroundSize,
          backgroundPosition: cs.backgroundPosition,
          backgroundRepeat: cs.backgroundRepeat,
          borderColor: cs.borderColor,
          borderWidth: px(cs.borderWidth),
          borderTopWidth: px(cs.borderTopWidth),
          borderTopColor: cs.borderTopColor,
          borderTopStyle: cs.borderTopStyle,
          borderRadius: px(cs.borderRadius),
          fontSize: px(cs.fontSize),
          fontFamily: cs.fontFamily,
          fontWeight: Number.parseInt(cs.fontWeight, 10) || cs.fontWeight,
          lineHeight: cs.lineHeight === 'normal' ? 1.2 : px(cs.lineHeight) / Math.max(px(cs.fontSize), 1),
          letterSpacing: cs.letterSpacing === 'normal' ? 0 : px(cs.letterSpacing),
          textAlign: cs.textAlign,
          verticalAlign: cs.verticalAlign,
          boxShadow: cs.boxShadow,
          filter: cs.filter,
          backdropFilter: cs.backdropFilter,
          padding: cs.padding,
          paddingLeft: px(cs.paddingLeft),
          paddingRight: px(cs.paddingRight),
          paddingTop: px(cs.paddingTop),
          paddingBottom: px(cs.paddingBottom),
          display: cs.display,
          flexDirection: cs.flexDirection,
          justifyContent: cs.justifyContent,
          alignItems: cs.alignItems,
          gridTemplateColumns: cs.gridTemplateColumns,
          gridTemplateRows: cs.gridTemplateRows,
          objectFit: cs.objectFit,
          objectPosition: cs.objectPosition,
          overflow: cs.overflow,
          overflowX: cs.overflowX,
          overflowY: cs.overflowY,
          whiteSpace: cs.whiteSpace,
          textOverflow: cs.textOverflow,
          writingMode: cs.writingMode,
          clipPath: cs.clipPath,
          maskImage: cs.maskImage,
          mixBlendMode: cs.mixBlendMode,
          transform: cs.transform,
          transformOrigin: cs.transformOrigin,
        }
      };
    });
    const scs = getComputedStyle(slide);
    const notes = slide.querySelector('[data-speaker-notes]');
    return {
      id: slide.dataset.slideId,
      name: slide.dataset.slideName || null,
      transition: slide.dataset.transition || 'none',
      stateOf: slide.dataset.stateOf || null,
      layout: slide.dataset.layout || null,
      background: scs.backgroundColor,
      backgroundStyle: {
        backgroundColor: scs.backgroundColor,
        backgroundImage: scs.backgroundImage,
        backgroundSize: scs.backgroundSize,
        backgroundPosition: scs.backgroundPosition,
        backgroundRepeat: scs.backgroundRepeat,
      },
      notes: notes ? notes.textContent.trim() : '',
      sourceWidth: sr.width,
      sourceHeight: sr.height,
      elements: elements.filter(element => element.tag !== 'aside')
    };
  });
}
"""


@dataclass(frozen=True)
class LayoutResult:
    slides: tuple[dict[str, Any], ...]
    source_screenshots: tuple[str, ...]
    image_fallbacks: dict[str, str]
    browser: str


def extract_computed_layout(
    chapters: list[SourceChapter],
    screenshots_dir: str | Path,
    *,
    browser_executable: str | Path | None = None,
) -> LayoutResult:
    """Open chapters in Chromium, then return normalized 1280x720 layout data."""

    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserCheckError("Playwright is required for HTML-first conversion. Install requirements-browser.txt.") from exc

    output = Path(screenshots_dir)
    output.mkdir(parents=True, exist_ok=True)
    all_slides: list[dict[str, Any]] = []
    screenshots: list[str] = []
    image_fallbacks: dict[str, str] = {}
    errors: list[str] = []
    executable = find_browser_executable(browser_executable)
    browser_label = executable.name if executable else "Playwright Chromium"
    try:
        with sync_playwright() as playwright:
            launch: dict[str, Any] = {"headless": True, "args": ["--allow-file-access-from-files"]}
            if executable:
                launch["executable_path"] = str(executable)
            browser = playwright.chromium.launch(**launch)
            page = browser.new_page(viewport={"width": 1400, "height": 900}, device_scale_factor=1)
            for chapter in chapters:
                page.goto(chapter.html_path.as_uri(), wait_until="load")
                page.add_style_tag(content="*,*::before,*::after{animation:none!important;transition:none!important;caret-color:transparent!important}")
                page.evaluate("document.fonts && document.fonts.ready")
                page.wait_for_timeout(100)
                computed = page.evaluate(EXTRACT_LAYOUT_JS)
                if not computed:
                    errors.append(issue(field="section.slide[data-slide-id]", actual=0, fix=f"Add at least one fixed-size slide to {chapter.html_path.name}."))
                    continue
                for slide_index, slide in enumerate(computed):
                    if abs(slide["sourceWidth"] - CANVAS_WIDTH) > 1 or abs(slide["sourceHeight"] - CANVAS_HEIGHT) > 1:
                        errors.append(issue(slide_id=slide["id"], field="computed size", actual=(slide["sourceWidth"], slide["sourceHeight"]), fix="Render every source slide at exactly 1280x720 CSS pixels."))
                    slide["chapterId"] = chapter.chapter_id
                    all_slides.append(slide)
                    slide_locator = page.locator(f'section.slide[data-slide-id="{slide["id"]}"]')
                    target = output / f"{len(all_slides):02d}-{slide['id']}.png"
                    slide_locator.screenshot(path=str(target))
                    screenshots.append(str(target.resolve()))
                    for element in slide["elements"]:
                        compatibility = classify_native_compatibility(element)
                        element["compatibility"] = {
                            "classification": compatibility.classification,
                            "reasons": list(compatibility.reasons),
                            "adjustments": list(compatibility.adjustments),
                        }
                        capture_reasons = [
                            reason for reason, active in (
                                ("explicit-image-fallback", element["exportMode"] == "image"),
                                ("image-required", compatibility.classification == "image-required"),
                                ("skew-transform", element.get("transform", {}).get("hasSkew")),
                                ("3d-transform", element.get("transform", {}).get("is3D")),
                            ) if active
                        ]
                        capture_needed = bool(capture_reasons)
                        if not capture_needed:
                            continue
                        capture_reason = ", ".join(capture_reasons)
                        if not element.get("explicitId"):
                            errors.append(issue(
                                slide_id=slide["id"], element_id=element["id"], field="fallback capture",
                                actual={"captureReason": capture_reason, "matchedElementCount": 0},
                                fix="Add exactly one explicit data-bento-id to the fallback element inside this slide.",
                            ))
                            continue
                        explicit = slide_locator.locator(f'[data-bento-id="{element["id"]}"]')
                        matched_count = explicit.count()
                        if matched_count != 1:
                            errors.append(issue(
                                slide_id=slide["id"], element_id=element["id"], field="fallback capture",
                                actual={"captureReason": capture_reason, "matchedElementCount": matched_count},
                                fix="Use this data-bento-id exactly once inside the current slide; duplicate ids are allowed only on different slides for morph.",
                            ))
                            continue
                        payload = explicit.screenshot(type="png")
                        image_fallbacks[f"{slide['id']}/{element['id']}"] = "data:image/png;base64," + base64.b64encode(payload).decode("ascii")
            browser.close()
    except PlaywrightError as exc:
        raise BrowserCheckError(f"Chromium layout extraction failed: {exc}") from exc
    if errors:
        raise ValidationError(errors)
    return LayoutResult(tuple(all_slides), tuple(screenshots), image_fallbacks, browser_label)
