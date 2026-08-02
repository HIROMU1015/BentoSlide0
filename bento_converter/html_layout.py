"""Render HTML chapters in Chromium and extract deterministic computed layout."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .browser_check import find_browser_executable
from .errors import BrowserCheckError, ValidationError, issue
from .html_source import SourceChapter

CANVAS_WIDTH = 1280
CANVAS_HEIGHT = 720

EXTRACT_LAYOUT_JS = r"""
() => {
  const px = value => {
    const parsed = Number.parseFloat(value);
    return Number.isFinite(parsed) ? parsed : 0;
  };
  const rotation = transform => {
    if (!transform || transform === 'none') return 0;
    const match = transform.match(/^matrix\(([^)]+)\)$/);
    if (!match) return 0;
    const [a, b] = match[1].split(',').map(Number);
    return Math.round(Math.atan2(b, a) * 180 / Math.PI * 1000) / 1000;
  };
  const slides = [...document.querySelectorAll('section.slide[data-slide-id]')];
  return slides.map((slide, slideIndex) => {
    const sr = slide.getBoundingClientRect();
    const sx = 1280 / sr.width;
    const sy = 720 / sr.height;
    const explicit = [...slide.querySelectorAll('[data-bento-id],[data-bento-type],[data-bento-export]')];
    const implicit = [...slide.querySelectorAll('h1,h2,h3,h4,h5,h6,p,span,li,table,img,svg,video,audio')]
      .filter(el => !el.closest('[data-bento-id],[data-bento-type],[data-bento-export]') && !el.parentElement?.closest('h1,h2,h3,h4,h5,h6,p,span,li,table'));
    const candidates = [...explicit, ...implicit];
    const elements = candidates.map((el, index) => {
      const cs = getComputedStyle(el);
      const r = el.getBoundingClientRect();
      const tag = el.tagName.toLowerCase();
      const explicitType = el.dataset.bentoType;
      let inferred = explicitType || 'text';
      if (!explicitType && tag === 'table') inferred = 'table';
      if (!explicitType && tag === 'img') inferred = 'image';
      if (!explicitType && tag === 'svg') inferred = 'svg';
      if (!explicitType && (tag === 'video' || tag === 'audio')) inferred = 'media';
      const chartScript = el.querySelector?.('script[type="application/json"][data-bento-chart],script[type="application/json"][data-chart-option]');
      let chartOption = null;
      if (chartScript) {
        try { chartOption = JSON.parse(chartScript.textContent); } catch (_) {}
      }
      const table = tag === 'table' || inferred === 'table' ? {
        rows: [...el.querySelectorAll('tr')].map(row => [...row.cells].map(cell => {
          const cellStyle = getComputedStyle(cell);
          return {
            html: cell.innerHTML,
            align: cellStyle.textAlign,
            color: cellStyle.color,
            bg: cellStyle.backgroundColor,
            bold: Number.parseInt(cellStyle.fontWeight, 10) >= 600,
          };
        })),
        columnWidths: [...(el.querySelector('tr')?.cells || [])].map(cell => cell.getBoundingClientRect().width),
        headerRows: el.querySelectorAll('thead tr').length,
      } : null;
      const layout = el.closest('[data-layout]');
      const exportMode = el.dataset.bentoExport || 'auto';
      return {
        id: el.dataset.bentoId || `auto-${slideIndex + 1}-${index + 1}`,
        domIndex: index,
        tag,
        type: inferred,
        exportMode,
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
        x: (r.left - sr.left) * sx,
        y: (r.top - sr.top) * sy,
        w: r.width * sx,
        h: r.height * sy,
        scrollWidth: el.scrollWidth * sx,
        scrollHeight: el.scrollHeight * sy,
        rotation: rotation(cs.transform),
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
        style: {
          color: cs.color,
          backgroundColor: cs.backgroundColor,
          backgroundImage: cs.backgroundImage,
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
          padding: cs.padding,
          paddingLeft: px(cs.paddingLeft),
          paddingRight: px(cs.paddingRight),
          paddingTop: px(cs.paddingTop),
          paddingBottom: px(cs.paddingBottom),
          display: cs.display,
          justifyContent: cs.justifyContent,
          alignItems: cs.alignItems,
          objectFit: cs.objectFit,
          objectPosition: cs.objectPosition,
          overflow: cs.overflow,
          clipPath: cs.clipPath,
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
                    locator = page.locator(f'section.slide[data-slide-id="{slide["id"]}"]')
                    target = output / f"{len(all_slides):02d}-{slide['id']}.png"
                    locator.screenshot(path=str(target))
                    screenshots.append(str(target.resolve()))
                    for element in slide["elements"]:
                        if element["exportMode"] != "image":
                            continue
                        explicit = page.locator(f'[data-bento-id="{element["id"]}"]').first
                        if explicit.count() != 1:
                            errors.append(issue(slide_id=slide["id"], element_id=element["id"], field="data-bento-export", actual="image", fix="Image fallback requires an explicit unique data-bento-id."))
                            continue
                        payload = explicit.screenshot(type="png")
                        image_fallbacks[f"{slide['id']}/{element['id']}"] = "data:image/png;base64," + base64.b64encode(payload).decode("ascii")
            browser.close()
    except PlaywrightError as exc:
        raise BrowserCheckError(f"Chromium layout extraction failed: {exc}") from exc
    if errors:
        raise ValidationError(errors)
    return LayoutResult(tuple(all_slides), tuple(screenshots), image_fallbacks, browser_label)
