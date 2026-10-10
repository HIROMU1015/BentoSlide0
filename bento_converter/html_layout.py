"""Render HTML chapters in Chromium and extract deterministic computed layout."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .browser_harness import BrowserHarness
from .errors import BrowserCheckError, ValidationError, issue
from .html_source import SourceChapter
from .incremental_cache import (
    IncrementalSlideCache,
    SourceFingerprintInput,
    compute_source_fingerprint_inputs,
    source_cache_key,
)
from .native_compatibility import classify_native_compatibility

CANVAS_WIDTH = 1280
CANVAS_HEIGHT = 720

EXTRACT_LAYOUT_JS = r"""
slideIds => {
  const px = value => {
    const parsed = Number.parseFloat(value);
    return Number.isFinite(parsed) ? parsed : 0;
  };
  const finiteNumber = (value, fallback) => {
    const parsed = Number.parseFloat(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  };
  const borderFields = style => ({
    borderTopWidth: px(style.borderTopWidth), borderTopColor: style.borderTopColor, borderTopStyle: style.borderTopStyle,
    borderRightWidth: px(style.borderRightWidth), borderRightColor: style.borderRightColor, borderRightStyle: style.borderRightStyle,
    borderBottomWidth: px(style.borderBottomWidth), borderBottomColor: style.borderBottomColor, borderBottomStyle: style.borderBottomStyle,
    borderLeftWidth: px(style.borderLeftWidth), borderLeftColor: style.borderLeftColor, borderLeftStyle: style.borderLeftStyle,
  });
  let pseudoProbeSequence = 0;
  const visiblePseudo = pseudo => (
    pseudo.content && !['none','normal'].includes(pseudo.content)
    && pseudo.display !== 'none'
    && finiteNumber(pseudo.opacity, 1) > 0
  );
  const emptyPseudoContent = content => ['""', "''"].includes(String(content || '').trim());
  const measurePseudoDecorations = (owner, sr, sx, sy) => {
    const selector = '[data-bento-id],[data-bento-type],[data-bento-export]';
    const hosts = [owner, ...owner.querySelectorAll('*')]
      .filter(host => host.closest(selector) === owner);
    const decorations = [];
    const reservations = [];
    let uncaptured = false;
    hosts.forEach((host, hostIndex) => {
      ['before', 'after'].forEach(which => {
        const pseudo = getComputedStyle(host, `::${which}`);
        if (!visiblePseudo(pseudo)) return;
        if (!emptyPseudoContent(pseudo.content)) {
          uncaptured = true;
          return;
        }
        const token = `p${++pseudoProbeSequence}`;
        const previousToken = host.getAttribute('data-bento-pseudo-probe');
        const suppress = document.createElement('style');
        suppress.textContent = `[data-bento-pseudo-probe="${token}"]::${which}{content:none!important}`;
        const probe = document.createElement('span');
        for (let propertyIndex = 0; propertyIndex < pseudo.length; propertyIndex += 1) {
          const property = pseudo.item(propertyIndex);
          probe.style.setProperty(property, pseudo.getPropertyValue(property), pseudo.getPropertyPriority(property));
        }
        probe.style.removeProperty('content');
        probe.setAttribute('aria-hidden', 'true');
        host.setAttribute('data-bento-pseudo-probe', token);
        document.head.appendChild(suppress);
        if (which === 'before') host.insertBefore(probe, host.firstChild);
        else host.appendChild(probe);
        const rect = probe.getBoundingClientRect();
        const probeStyle = getComputedStyle(probe);
        if (rect.width > 0 && rect.height > 0) {
          decorations.push({
            idSuffix: `pseudo-${hostIndex + 1}-${which}`,
            x: (rect.left - sr.left) * sx, y: (rect.top - sr.top) * sy,
            w: rect.width * sx, h: rect.height * sy,
            opacity: finiteNumber(probeStyle.opacity, 1),
            style: {
              backgroundColor: probeStyle.backgroundColor,
              borderColor: probeStyle.borderColor,
              borderWidth: px(probeStyle.borderWidth),
              ...borderFields(probeStyle),
              borderRadius: px(probeStyle.borderRadius),
              boxShadow: probeStyle.boxShadow,
            },
          });
          const participatesInInlineFlow = (
            !['absolute', 'fixed'].includes(probeStyle.position)
            && ['inline', 'inline-block', 'inline-flex', 'inline-grid'].includes(probeStyle.display)
          );
          if (participatesInInlineFlow) {
            const targetAdvance = rect.width + px(probeStyle.marginLeft) + px(probeStyle.marginRight);
            const spaceProbe = document.createElement('span');
            spaceProbe.textContent = '\u00a0';
            spaceProbe.setAttribute('style', 'position:absolute;visibility:hidden;white-space:pre;padding:0;border:0;margin:0');
            host.appendChild(spaceProbe);
            const spaceWidth = spaceProbe.getBoundingClientRect().width;
            spaceProbe.remove();
            reservations.push({
              hostIndex,
              which,
              // Bento's editable text runtime may render NBSP narrower than
              // Chromium's source font. Two guard spaces preserve the visual
              // margin without shifting subsequent lines in the label.
              text: '\u00a0'.repeat(Math.max(1, Math.ceil(targetAdvance / Math.max(spaceWidth, 0.1))) + 2),
            });
          }
        } else {
          uncaptured = true;
        }
        probe.remove();
        suppress.remove();
        if (previousToken === null) host.removeAttribute('data-bento-pseudo-probe');
        else host.setAttribute('data-bento-pseudo-probe', previousToken);
      });
    });
    return {decorations, reservations, uncaptured};
  };
  const preserveOwnedRichTextStyles = (owner, clone) => {
    const selector = '[data-bento-id],[data-bento-type],[data-bento-export]';
    const sourceHosts = [owner, ...owner.querySelectorAll('*')]
      .filter(host => host.closest(selector) === owner);
    const clonedHosts = [clone, ...clone.querySelectorAll('*')]
      .filter(host => host.closest(selector) === clone);
    const inheritedProperties = [
      'font-size', 'font-family', 'font-weight', 'font-style', 'line-height',
      'letter-spacing', 'color', 'text-align', 'white-space', 'text-transform',
      'text-decoration-line', 'text-decoration-color', 'text-decoration-style',
      'vertical-align',
    ];
    let changed = false;
    sourceHosts.forEach((source, index) => {
      if (source === owner) return;
      const target = clonedHosts[index];
      if (!(target instanceof HTMLElement)) return;
      const computed = getComputedStyle(source);
      const parentComputed = source.parentElement ? getComputedStyle(source.parentElement) : null;
      const defaultDisplay = ['DIV', 'P'].includes(source.tagName) ? 'block' : 'inline';
      if (computed.display !== defaultDisplay && computed.display !== 'contents') {
        target.style.setProperty('display', computed.display);
        changed = true;
      }
      ['margin-top', 'margin-right', 'margin-bottom', 'margin-left'].forEach(property => {
        const value = computed.getPropertyValue(property);
        if (Math.abs(px(value)) > 0.001) {
          target.style.setProperty(property, value);
          changed = true;
        }
      });
      inheritedProperties.forEach(property => {
        const value = computed.getPropertyValue(property);
        const inherited = parentComputed?.getPropertyValue(property);
        if (value && value !== inherited) {
          target.style.setProperty(property, value);
          changed = true;
        }
      });
    });
    return changed;
  };
  const htmlWithPseudoReservations = (owner, pseudoCapture) => {
    const selector = '[data-bento-id],[data-bento-type],[data-bento-export]';
    const clone = owner.cloneNode(true);
    const hosts = [clone, ...clone.querySelectorAll('*')]
      .filter(host => host.closest(selector) === clone);
    let changed = preserveOwnedRichTextStyles(owner, clone);
    pseudoCapture.reservations.forEach(reservation => {
      const host = hosts[reservation.hostIndex];
      if (!host) return;
      const spacer = document.createTextNode(reservation.text);
      if (reservation.which === 'before') host.insertBefore(spacer, host.firstChild);
      else host.appendChild(spacer);
      changed = true;
    });
    return changed ? clone.innerHTML : null;
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
  const textRects = owner => {
    const selector = '[data-bento-id],[data-bento-type],[data-bento-export]';
    const rects = [];
    const walker = document.createTreeWalker(owner, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const node = walker.currentNode;
      const value = node.textContent || '';
      const start = value.search(/\S/);
      if (start < 0) continue;
      const nearestAnnotated = node.parentElement?.closest(selector);
      if (nearestAnnotated && nearestAnnotated !== owner && owner.contains(nearestAnnotated)) continue;
      const end = value.length - (value.match(/\s*$/)?.[0].length || 0);
      if (end <= start) continue;
      const range = document.createRange();
      range.setStart(node, start);
      range.setEnd(node, end);
      for (const rect of range.getClientRects()) {
        if (rect.width > 0 && rect.height > 0) rects.push(rect);
      }
    }
    return rects;
  };
  const measuredLines = (rects, sx, sy, originLeft, originTop) => {
    const ordered = [...rects].sort((a, b) => a.top - b.top || a.left - b.left);
    const lines = [];
    ordered.forEach(rect => {
      const center = (rect.top + rect.bottom) / 2;
      let line = lines.find(item => center >= item.top - 1 && center <= item.bottom + 1);
      if (!line) {
        line = {left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom, fragments: 0};
        lines.push(line);
      } else {
        line.left = Math.min(line.left, rect.left);
        line.top = Math.min(line.top, rect.top);
        line.right = Math.max(line.right, rect.right);
        line.bottom = Math.max(line.bottom, rect.bottom);
      }
      line.fragments += 1;
    });
    return lines.sort((a, b) => a.top - b.top || a.left - b.left).map((line, index) => ({
      index,
      x: (line.left - originLeft) * sx,
      y: (line.top - originTop) * sy,
      w: (line.right - line.left) * sx,
      h: (line.bottom - line.top) * sy,
      fragments: line.fragments,
    }));
  };
  const rightBoundaryFor = (owner, rect, peers, container) => {
    const containerRect = container.getBoundingClientRect();
    const containerStyle = getComputedStyle(container);
    let boundary = containerRect.right
      - px(containerStyle.paddingRight)
      - px(containerStyle.borderRightWidth);
    peers.forEach(peer => {
      if (peer === owner || owner.contains(peer) || peer.contains(owner)) return;
      const peerRect = peer.getBoundingClientRect();
      const verticalOverlap = Math.min(rect.bottom, peerRect.bottom) - Math.max(rect.top, peerRect.top);
      if (verticalOverlap <= 1 || peerRect.left < rect.right - 0.5) return;
      boundary = Math.min(boundary, peerRect.left);
    });
    return Math.max(rect.right, boundary);
  };
  const measureTextLayout = (owner, rect, style, rightBoundary, sx, sy) => {
    if (style.transform !== 'none') return null;
    const rects = textRects(owner);
    if (!rects.length) return null;
    const fontSize = Math.max(1, px(style.fontSize));
    const lineHeight = style.lineHeight === 'normal' ? fontSize * 1.2 : Math.max(fontSize, px(style.lineHeight));
    const top = Math.min(...rects.map(item => item.top));
    const bottom = Math.max(...rects.map(item => item.bottom));
    const contentLeft = rect.left + px(style.paddingLeft) + px(style.borderLeftWidth);
    const contentWidth = Math.max(
      0,
      rect.width
        - px(style.paddingLeft) - px(style.paddingRight)
        - px(style.borderLeftWidth) - px(style.borderRightWidth),
    );
    const lines = measuredLines(rects, sx, sy, rect.left, rect.top);
    return {
      singleLine: lines.length <= 1 && bottom - top <= lineHeight * 1.5,
      requiredWidth: Math.max(0, Math.max(...rects.map(item => item.right)) - contentLeft) * sx,
      maxWidth: Math.max(contentWidth, rightBoundary - contentLeft) * sx,
      sourceContentWidth: contentWidth * sx,
      measuredHeight: (bottom - top) * sy,
      lineCount: lines.length,
      lines,
    };
  };
  const requested = new Set(Array.isArray(slideIds) ? slideIds : []);
  const slides = [...document.querySelectorAll('section.slide[data-slide-id]')];
  return slides.map((slide, slideIndex) => ({slide, slideIndex}))
    .filter(item => requested.size === 0 || requested.has(item.slide.dataset.slideId))
    .map(({slide, slideIndex}) => {
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
              lineHeight: cellStyle.lineHeight === 'normal' ? 1.2 : px(cellStyle.lineHeight) / Math.max(px(cellStyle.fontSize), 1),
              whiteSpace: cellStyle.whiteSpace,
              ...borderFields(cellStyle),
              borderRadius: px(cellStyle.borderRadius),
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
      const pseudoCapture = measurePseudoDecorations(el, sr, sx, sy);
      const pseudoReservedHtml = htmlWithPseudoReservations(el, pseudoCapture);
      const pseudoElementDependent = pseudoCapture.uncaptured;
      const explicitDescendants = [...el.querySelectorAll('[data-bento-id],[data-bento-type],[data-bento-export]')];
      const residual = el.cloneNode(true);
      residual.querySelectorAll('[data-bento-id],[data-bento-type],[data-bento-export]').forEach(child => child.remove());
      const residualTextNodes = [];
      const residualWalker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
      while (residualWalker.nextNode()) {
        const node = residualWalker.currentNode;
        const owner = node.parentElement?.closest('[data-bento-id],[data-bento-type],[data-bento-export]');
        if (node.textContent.trim() && owner === el) residualTextNodes.push(node);
      }
      let residualOwner = residualTextNodes[0]?.parentElement || null;
      while (residualOwner && residualOwner !== el && residualTextNodes.some(node => !residualOwner.contains(node))) {
        residualOwner = residualOwner.parentElement;
      }
      const residualRect = residualOwner && residualOwner !== el ? residualOwner.getBoundingClientRect() : null;
      const residualComputed = residualOwner && residualOwner !== el ? getComputedStyle(residualOwner) : null;
      const textLayout = measureTextLayout(
        el, r, cs, rightBoundaryFor(el, r, candidates, slide), sx, sy,
      );
      const equationMetrics = inferred === 'equation'
        ? (() => {
            const preferred = el.querySelector('.katex-html,mjx-container,math');
            let glyphRect = preferred?.getBoundingClientRect() || null;
            if (!glyphRect || glyphRect.width <= 0 || glyphRect.height <= 0) {
              const rects = textRects(el);
              if (rects.length) {
                const left = Math.min(...rects.map(item => item.left));
                const top = Math.min(...rects.map(item => item.top));
                const right = Math.max(...rects.map(item => item.right));
                const bottom = Math.max(...rects.map(item => item.bottom));
                glyphRect = {left, top, right, bottom, width: right - left, height: bottom - top};
              }
            }
            if (!glyphRect || glyphRect.width <= 0 || glyphRect.height <= 0) return null;
            return {
              glyphFrame: {
                x: (glyphRect.left - sr.left) * sx,
                y: (glyphRect.top - sr.top) * sy,
                w: glyphRect.width * sx,
                h: glyphRect.height * sy,
              },
              elementFrame: {x: tx.frame.x, y: tx.frame.y, w: tx.frame.w, h: tx.frame.h},
              renderer: preferred?.matches('.katex-html') ? 'katex'
                : preferred?.matches('mjx-container') ? 'mathjax'
                : preferred?.matches('math') ? 'mathml'
                : 'browser-text',
            };
          })()
        : null;
      const textFragments = inferred === 'text' && ['flex', 'inline-flex'].includes(cs.display)
        ? [...el.children].filter(child => !child.matches('[data-bento-id],[data-bento-type],[data-bento-export]'))
          .map((child, fragmentIndex) => {
            const childRect = child.getBoundingClientRect();
            const childStyle = getComputedStyle(child);
            const fragmentPeers = [...el.children]
              .filter(peer => !peer.matches('[data-bento-id],[data-bento-type],[data-bento-export]'));
            return {
              index: fragmentIndex,
              partId: child.dataset.bentoPart || null,
              text: child.textContent.trim(),
              html: child.innerHTML,
              x:(childRect.left-sr.left)*sx, y:(childRect.top-sr.top)*sy,
              w:childRect.width*sx, h:childRect.height*sy,
              scrollWidth: child.scrollWidth*sx, scrollHeight: child.scrollHeight*sy,
              textLayout: measureTextLayout(
                child, childRect, childStyle,
                rightBoundaryFor(child, childRect, fragmentPeers, el), sx, sy,
              ),
              style: {
                color: childStyle.color,
                fontSize: px(childStyle.fontSize),
                fontFamily: childStyle.fontFamily,
                fontWeight: Number.parseInt(childStyle.fontWeight, 10) || childStyle.fontWeight,
                lineHeight: childStyle.lineHeight === 'normal' ? 1.2 : px(childStyle.lineHeight) / Math.max(px(childStyle.fontSize), 1),
                letterSpacing: childStyle.letterSpacing === 'normal' ? 0 : px(childStyle.letterSpacing),
                textAlign: childStyle.textAlign,
                verticalAlign: childStyle.verticalAlign,
                display: childStyle.display,
                paddingLeft: px(childStyle.paddingLeft), paddingRight: px(childStyle.paddingRight),
                paddingTop: px(childStyle.paddingTop), paddingBottom: px(childStyle.paddingBottom),
                backgroundColor: childStyle.backgroundColor, backgroundImage: childStyle.backgroundImage,
                borderColor: childStyle.borderColor, borderWidth: px(childStyle.borderWidth),
                borderRadius: px(childStyle.borderRadius), boxShadow: childStyle.boxShadow,
              },
            };
          }).filter(fragment => fragment.text)
        : [];
      const ownedTextFragments = inferred === 'shape' && explicitDescendants.length === 0
        ? (() => {
          const candidates = [...el.children].filter(child => (
            !child.matches('[data-bento-id],[data-bento-type],[data-bento-export]')
            && child.tagName !== 'BR'
            && child.textContent.trim()
          ));
          if (!candidates.length) {
            return [];
          }
          const hasUnownedText = residualTextNodes.some(
            node => !candidates.some(child => child.contains(node)),
          );
          const blockDisplays = new Set(['block', 'flex', 'grid', 'list-item', 'table', 'flow-root']);
          const hasBlockChild = candidates.some(child => blockDisplays.has(getComputedStyle(child).display));
          if (hasUnownedText && !hasBlockChild) return [];

          const fragmentFor = (nodes, fragmentIndex, {blockChild = null, styledNodes = null, directIndex = null}) => {
            const range = document.createRange();
            if (blockChild) range.selectNodeContents(blockChild);
            else {
              range.setStartBefore(nodes[0]);
              range.setEndAfter(nodes[nodes.length - 1]);
            }
            const rangeRects = [...range.getClientRects()].filter(
              rect => rect.width > 0 && rect.height > 0,
            );
            if (!rangeRects.length) return null;
            const rangeRect = {
              left: Math.min(...rangeRects.map(rect => rect.left)),
              top: Math.min(...rangeRects.map(rect => rect.top)),
              right: Math.max(...rangeRects.map(rect => rect.right)),
              bottom: Math.max(...rangeRects.map(rect => rect.bottom)),
            };
            rangeRect.width = rangeRect.right - rangeRect.left;
            rangeRect.height = rangeRect.bottom - rangeRect.top;
            const childStyle = getComputedStyle(blockChild || el);
            const ownerContentRight = r.right - px(cs.paddingRight) - px(cs.borderRightWidth);
            const fontSize = Math.max(1, px(childStyle.fontSize));
            const lineHeight = childStyle.lineHeight === 'normal'
              ? fontSize * 1.2
              : Math.max(fontSize, px(childStyle.lineHeight));
            const top = Math.min(...rangeRects.map(rect => rect.top));
            const bottom = Math.max(...rangeRects.map(rect => rect.bottom));
            const html = blockChild
              ? blockChild.outerHTML
              : (() => {
                  const wrapper = document.createElement('div');
                  nodes.forEach(node => wrapper.appendChild(styledNodes[directIndex.get(node)].cloneNode(true)));
                  return wrapper.innerHTML;
                })();
            const partHost = blockChild || (
              nodes.length === 1 && nodes[0] instanceof HTMLElement ? nodes[0] : null
            );
            const lines = measuredLines(rangeRects, sx, sy, rangeRect.left, rangeRect.top);
            return {
              index: fragmentIndex,
              partId: partHost?.dataset.bentoPart || null,
              text: nodes.map(node => node.textContent || '').join('').trim(),
              html,
              x:(rangeRect.left-sr.left)*sx, y:(rangeRect.top-sr.top)*sy,
              w:rangeRect.width*sx, h:rangeRect.height*sy,
              scrollWidth:rangeRect.width*sx, scrollHeight:rangeRect.height*sy,
              textLayout: {
                singleLine: lines.length <= 1 && bottom - top <= lineHeight * 1.5,
                requiredWidth: Math.max(0, Math.max(...rangeRects.map(rect => rect.right)) - rangeRect.left) * sx,
                maxWidth: Math.max(rangeRect.width, ownerContentRight - rangeRect.left) * sx,
                sourceContentWidth: rangeRect.width * sx,
                measuredHeight: rangeRect.height * sy,
                lineCount: lines.length,
                lines,
              },
              style: {
                color: childStyle.color,
                fontSize: px(childStyle.fontSize),
                fontFamily: childStyle.fontFamily,
                fontWeight: Number.parseInt(childStyle.fontWeight, 10) || childStyle.fontWeight,
                lineHeight: childStyle.lineHeight === 'normal' ? 1.2 : px(childStyle.lineHeight) / Math.max(px(childStyle.fontSize), 1),
                letterSpacing: childStyle.letterSpacing === 'normal' ? 0 : px(childStyle.letterSpacing),
                textAlign: blockChild ? childStyle.textAlign : cs.textAlign,
                verticalAlign: childStyle.verticalAlign,
                display: childStyle.display,
                paddingLeft: 0, paddingRight: 0, paddingTop: 0, paddingBottom: 0,
                backgroundColor: 'transparent', backgroundImage: 'none',
                borderColor: 'transparent', borderWidth: 0, borderRadius: 0, boxShadow: 'none',
              },
            };
          };

          if (hasUnownedText) {
            const directNodes = [...el.childNodes];
            const directIndex = new Map(directNodes.map((node, index) => [node, index]));
            const styledHost = document.createElement(el.tagName);
            styledHost.innerHTML = pseudoReservedHtml || el.innerHTML;
            const styledNodes = [...styledHost.childNodes];
            const groups = [];
            let inlineGroup = [];
            const flushInline = () => {
              if (inlineGroup.some(node => (node.textContent || '').trim())) groups.push({nodes: inlineGroup});
              inlineGroup = [];
            };
            directNodes.forEach(node => {
              const blockChild = node instanceof HTMLElement
                && !node.matches('[data-bento-id],[data-bento-type],[data-bento-export]')
                && blockDisplays.has(getComputedStyle(node).display)
                && (node.textContent || '').trim();
              if (blockChild) {
                flushInline();
                groups.push({nodes: [node], blockChild: node});
              } else {
                inlineGroup.push(node);
              }
            });
            flushInline();
            return groups.map((group, fragmentIndex) => fragmentFor(
              group.nodes, fragmentIndex, {
                blockChild: group.blockChild || null,
                styledNodes,
                directIndex,
              },
            )).filter(fragment => fragment && fragment.text);
          }

          return candidates.map((child, fragmentIndex) => {
            const range = document.createRange();
            range.selectNodeContents(child);
            const rangeRect = range.getBoundingClientRect();
            const childRect = rangeRect.width > 0 && rangeRect.height > 0
              ? rangeRect
              : child.getBoundingClientRect();
            const childStyle = getComputedStyle(child);
            const measuredWidth = childRect.width * sx;
            return {
              index: fragmentIndex,
              partId: child.dataset.bentoPart || null,
              text: child.textContent.trim(),
              html: child.outerHTML,
              x:(childRect.left-sr.left)*sx, y:(childRect.top-sr.top)*sy,
              w:measuredWidth, h:childRect.height*sy,
              scrollWidth: measuredWidth, scrollHeight: childRect.height*sy,
              textLayout: measureTextLayout(
                child, childRect, childStyle,
                rightBoundaryFor(child, childRect, candidates, el), sx, sy,
              ),
              style: {
                color: childStyle.color,
                fontSize: px(childStyle.fontSize),
                fontFamily: childStyle.fontFamily,
                fontWeight: Number.parseInt(childStyle.fontWeight, 10) || childStyle.fontWeight,
                lineHeight: childStyle.lineHeight === 'normal' ? 1.2 : px(childStyle.lineHeight) / Math.max(px(childStyle.fontSize), 1),
                letterSpacing: childStyle.letterSpacing === 'normal' ? 0 : px(childStyle.letterSpacing),
                textAlign: 'left',
                verticalAlign: childStyle.verticalAlign,
                display: childStyle.display,
                paddingLeft: 0, paddingRight: 0, paddingTop: 0, paddingBottom: 0,
                backgroundColor: 'transparent', backgroundImage: 'none',
                borderColor: 'transparent', borderWidth: 0, borderRadius: 0, boxShadow: 'none',
              },
            };
          });
        })()
        : [];
      const ancestorIds = [];
      for (let parent = el.parentElement; parent && parent !== slide; parent = parent.parentElement) {
        if (parent.matches('[data-bento-id],[data-bento-type],[data-bento-export]') && parent.dataset.bentoId) {
          ancestorIds.push(parent.dataset.bentoId);
        }
      }
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
        opacity: finiteNumber(cs.opacity, 1),
        z: Number.isFinite(Number.parseInt(el.dataset.bentoZ, 10)) ? Number.parseInt(el.dataset.bentoZ, 10) : (Number.parseInt(cs.zIndex, 10) || index),
        text: el.textContent.trim(),
        html: el.innerHTML,
        htmlWithPseudoReservations: pseudoReservedHtml,
        residualText: residual.textContent.trim(),
        residualHtml: residual.innerHTML,
        residualFrame: residualRect ? {
          x:(residualRect.left-sr.left)*sx, y:(residualRect.top-sr.top)*sy,
          w:residualRect.width*sx, h:residualRect.height*sy,
        } : null,
        residualStyle: residualComputed ? {
          color: residualComputed.color,
          fontSize: px(residualComputed.fontSize),
          fontFamily: residualComputed.fontFamily,
          fontWeight: Number.parseInt(residualComputed.fontWeight, 10) || residualComputed.fontWeight,
          lineHeight: residualComputed.lineHeight === 'normal' ? 1.2 : px(residualComputed.lineHeight) / Math.max(px(residualComputed.fontSize), 1),
          letterSpacing: residualComputed.letterSpacing === 'normal' ? 0 : px(residualComputed.letterSpacing),
          textAlign: residualComputed.textAlign,
          verticalAlign: residualComputed.verticalAlign,
          display: residualComputed.display,
          flexDirection: residualComputed.flexDirection,
          justifyContent: residualComputed.justifyContent,
          alignItems: residualComputed.alignItems,
          paddingLeft: 0, paddingRight: 0, paddingTop: 0, paddingBottom: 0,
          backgroundColor: 'transparent', backgroundImage: 'none', borderWidth: 0, boxShadow: 'none',
        } : null,
        textLayout,
        equationMetrics,
        textFragments,
        ownedTextFragments,
        outerHTML: el.outerHTML,
        svg: tag === 'svg' ? el.outerHTML : el.querySelector?.(':scope > svg')?.outerHTML || null,
        src: el.currentSrc || el.src || null,
        poster: el.getAttribute('poster') || el.poster || null,
        mediaKind: tag === 'audio' ? 'audio' : 'video',
        controls: Boolean(el.controls),
        autoplay: Boolean(el.autoplay),
        loop: Boolean(el.loop),
        muted: Boolean(el.muted),
        chartOption,
        table,
        pseudoElementDependent,
        pseudoDecorations: pseudoCapture.decorations,
        ancestorIds,
        hasExplicitDescendants: explicitDescendants.length > 0,
        explicitDescendantIds: explicitDescendants.map(child => child.dataset.bentoId).filter(Boolean),
        fallbackScope: el.dataset.bentoFallbackScope || (
          explicitDescendants.length > 0 || residual.textContent.trim() ? 'background' : 'whole'
        ),
        style: {
          color: cs.color,
          backgroundColor: cs.backgroundColor,
          backgroundImage: cs.backgroundImage,
          backgroundSize: cs.backgroundSize,
          backgroundPosition: cs.backgroundPosition,
          backgroundRepeat: cs.backgroundRepeat,
          borderColor: cs.borderColor,
          borderWidth: px(cs.borderWidth),
          ...borderFields(cs),
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
    slide_fingerprints: dict[str, str] = field(default_factory=dict)
    cache_hits: tuple[str, ...] = ()


def extract_computed_layout(
    chapters: list[SourceChapter],
    screenshots_dir: str | Path,
    *,
    browser_executable: str | Path | None = None,
    harness: BrowserHarness | None = None,
    cache: IncrementalSlideCache | None = None,
    fingerprint_inputs: dict[str, SourceFingerprintInput] | None = None,
    runtime_fingerprint: str = "",
) -> LayoutResult:
    """Open chapters in Chromium, then return normalized 1280x720 layout data."""

    if harness is None:
        with BrowserHarness(browser_executable) as owned_harness:
            return extract_computed_layout(
                chapters,
                screenshots_dir,
                harness=owned_harness,
                cache=cache,
                fingerprint_inputs=fingerprint_inputs,
                runtime_fingerprint=runtime_fingerprint,
            )

    output = Path(screenshots_dir)
    output.mkdir(parents=True, exist_ok=True)
    all_slides: list[dict[str, Any]] = []
    screenshots: list[str] = []
    image_fallbacks: dict[str, str] = {}
    errors: list[str] = []
    fingerprints = fingerprint_inputs or compute_source_fingerprint_inputs(chapters)
    ordered_ids = list(fingerprints)
    positions = {slide_id: index for index, slide_id in enumerate(ordered_ids, start=1)}
    slide_fingerprints: dict[str, str] = {}
    cache_hits: list[str] = []
    try:
        with harness.page("sourceLayout") as page:
            for chapter in chapters:
                page.goto(chapter.html_path.as_uri(), wait_until="load")
                harness.settle(page)
                harness.assert_no_blocked_network("sourceLayout")
                harness.record_page_environment("sourceLayout", page)
                environment_digest = harness.profile_digest("sourceLayout")
                chapter_inputs = [
                    value for value in fingerprints.values() if value.chapter_id == chapter.chapter_id
                ]
                dirty_ids: list[str] = []
                chapter_slides: dict[str, dict[str, Any]] = {}
                for value in chapter_inputs:
                    key = source_cache_key(
                        value,
                        environment_digest=environment_digest,
                        runtime_fingerprint=runtime_fingerprint,
                    )
                    slide_fingerprints[value.slide_id] = key
                    target = output / f"{positions[value.slide_id]:02d}-{value.slide_id}.png"
                    cached = cache.load_source(value.slide_id, key, target) if cache else None
                    if cached is None:
                        dirty_ids.append(value.slide_id)
                        continue
                    slide, fallbacks = cached
                    chapter_slides[value.slide_id] = slide
                    image_fallbacks.update(fallbacks)
                    cache_hits.append(value.slide_id)
                computed = page.evaluate(EXTRACT_LAYOUT_JS, dirty_ids) if dirty_ids else []
                computed_by_id = {slide["id"]: slide for slide in computed}
                missing = [slide_id for slide_id in dirty_ids if slide_id not in computed_by_id]
                if missing:
                    errors.append(issue(
                        field="section.slide[data-slide-id]", actual=missing,
                        fix=f"Keep every discovered slide renderable in {chapter.html_path.name}.",
                    ))
                for slide_id in dirty_ids:
                    slide = computed_by_id.get(slide_id)
                    if slide is None:
                        continue
                    if abs(slide["sourceWidth"] - CANVAS_WIDTH) > 1 or abs(slide["sourceHeight"] - CANVAS_HEIGHT) > 1:
                        errors.append(issue(slide_id=slide["id"], field="computed size", actual=(slide["sourceWidth"], slide["sourceHeight"]), fix="Render every source slide at exactly 1280x720 CSS pixels."))
                    slide["chapterId"] = chapter.chapter_id
                    slide_locator = page.locator(f'section.slide[data-slide-id="{slide["id"]}"]')
                    target = output / f"{positions[slide_id]:02d}-{slide_id}.png"
                    slide_locator.screenshot(path=str(target))
                    slide_fallbacks: dict[str, str] = {}
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
                                ("background-image-required", compatibility.classification == "background-image-required"),
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
                        capture_scope = (
                            "background"
                            if compatibility.classification == "background-image-required"
                            else element.get("fallbackScope") or "whole"
                        )
                        capture_token = f"{slide['id']}/{element['id']}"
                        if capture_scope == "background":
                            page.evaluate(
                                """
                                ({slideId, elementId, token}) => {
                                  const slide = document.querySelector(`section.slide[data-slide-id="${CSS.escape(slideId)}"]`);
                                  const roots = slide ? [...slide.querySelectorAll('[data-bento-id]')]
                                    .filter(node => node.dataset.bentoId === elementId) : [];
                                  if (roots.length !== 1) throw new Error(`Background capture root count is ${roots.length}`);
                                  const root = roots[0];
                                  const selector = '[data-bento-id],[data-bento-type],[data-bento-export]';
                                  const records = [];
                                  const change = (node, properties) => {
                                    records.push({node, style: node.getAttribute('style')});
                                    for (const [name, value] of Object.entries(properties)) {
                                      node.style.setProperty(name, value, 'important');
                                    }
                                  };
                                  for (const descendant of root.querySelectorAll(selector)) {
                                    change(descendant, {visibility: 'hidden'});
                                  }
                                  for (const node of [root, ...root.querySelectorAll('*')]) {
                                    if (node !== root && node.closest(selector) !== root) continue;
                                    change(node, {
                                      color: 'transparent',
                                      '-webkit-text-fill-color': 'transparent',
                                      'text-shadow': 'none',
                                      'text-decoration-color': 'transparent',
                                      'caret-color': 'transparent',
                                    });
                                  }
                                  window.__bentoBackgroundCaptureRestores ||= new Map();
                                  window.__bentoBackgroundCaptureRestores.set(token, records);
                                }
                                """,
                                {"slideId": slide["id"], "elementId": element["id"], "token": capture_token},
                            )
                        try:
                            payload = explicit.screenshot(type="png")
                        finally:
                            if capture_scope == "background":
                                page.evaluate(
                                    """
                                    token => {
                                      const records = window.__bentoBackgroundCaptureRestores?.get(token) || [];
                                      for (const {node, style} of records.reverse()) {
                                        if (style === null) node.removeAttribute('style');
                                        else node.setAttribute('style', style);
                                      }
                                      window.__bentoBackgroundCaptureRestores?.delete(token);
                                    }
                                    """,
                                    capture_token,
                                )
                        fallback_key = f"{slide['id']}/{element['id']}"
                        fallback_value = "data:image/png;base64," + base64.b64encode(payload).decode("ascii")
                        image_fallbacks[fallback_key] = fallback_value
                        slide_fallbacks[fallback_key] = fallback_value
                        element["fallbackCapture"] = {
                            "scope": capture_scope,
                            "reasons": capture_reasons,
                            "excludedElementIds": list(element.get("explicitDescendantIds") or ()),
                            "ownedTextExcluded": capture_scope == "background" and bool(element.get("residualText")),
                        }
                    chapter_slides[slide_id] = slide
                    if cache:
                        cache.save_source(
                            slide_id,
                            slide_fingerprints[slide_id],
                            slide,
                            slide_fallbacks,
                            target,
                        )
                for value in chapter_inputs:
                    slide = chapter_slides.get(value.slide_id)
                    if slide is not None:
                        all_slides.append(slide)
                        screenshots.append(str((output / f"{positions[value.slide_id]:02d}-{value.slide_id}.png").resolve()))
    except BrowserCheckError:
        raise
    except Exception as exc:
        raise BrowserCheckError(f"Chromium layout extraction failed: {exc}") from exc
    if errors:
        raise ValidationError(errors)
    return LayoutResult(
        tuple(all_slides),
        tuple(screenshots),
        image_fallbacks,
        harness.browser_label,
        slide_fingerprints,
        tuple(cache_hits),
    )
