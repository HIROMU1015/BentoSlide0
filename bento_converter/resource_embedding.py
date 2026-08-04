"""Embed local fallback resources so generated Bento decks are portable."""

from __future__ import annotations

import base64
import html
import mimetypes
import os
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import unquote, urlparse

from .errors import ConversionError


ResourceLookup = Callable[[str], str]


def _data_uri(path: Path) -> tuple[str, str]:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{payload}", mime


def _is_windows_path(value: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", value)) or value.startswith("\\\\")


def _path_from_file_uri(value: str) -> Path:
    parsed = urlparse(value)
    path = unquote(parsed.path)
    if re.match(r"^/[A-Za-z]:/", path):
        path = path[1:]
    if parsed.netloc and parsed.netloc not in {"", "localhost"}:
        path = f"//{parsed.netloc}{path}"
    return Path(path)


def _is_local_resource(value: str) -> bool:
    normalized = value.strip()
    if not normalized or normalized.startswith(("data:", "#", "http:", "https:", "mailto:", "tel:")):
        return False
    return normalized.startswith(("file:", "asset:")) or _is_windows_path(normalized) or not bool(urlparse(normalized).scheme)


def _redacted_source(value: str, resolved: Path | None, source_root: Path) -> str:
    normalized = value.replace("\\", "/")
    if normalized.startswith("asset:"):
        return normalized
    if resolved is None:
        return "$SOURCE_ROOT/unknown"
    try:
        relative = resolved.resolve().relative_to(source_root.resolve()).as_posix()
        return f"$SOURCE_ROOT/{relative}"
    except ValueError:
        if not _is_windows_path(normalized) and not normalized.startswith("file:"):
            return f"$SOURCE_ROOT/{normalized.lstrip('./')}"
        return f"$EXTERNAL_SOURCE/{resolved.name}"


@dataclass
class ResourceContext:
    """Per-element source location and report sink for fallback embedding."""

    source_html_path: Path
    slide_id: str
    element_id: str
    asset_prefix: str
    records: list[dict[str, object]] = field(default_factory=list)
    asset_lookup: ResourceLookup | None = None

    @property
    def source_root(self) -> Path:
        return self.source_html_path.parent

    def for_element(self, element_id: str, asset_prefix: str | None = None) -> "ResourceContext":
        return ResourceContext(
            source_html_path=self.source_html_path,
            slide_id=self.slide_id,
            element_id=element_id,
            asset_prefix=asset_prefix or self.asset_prefix,
            records=self.records,
            asset_lookup=self.asset_lookup,
        )


def resolve_embedded_resource(value: str, *, context: ResourceContext) -> str:
    """Return a portable resource URL and record local-resource embedding."""

    original = html.unescape(value).strip()
    if not original or original.startswith(("data:", "#", "http:", "https:", "mailto:", "tel:")):
        return original
    if original.lower().startswith("javascript:"):
        raise ConversionError(f"Unsafe javascript resource reference in {context.slide_id}/{context.element_id}.")
    if original.startswith("asset:"):
        asset_id = original[len("asset:"):]
        if not asset_id or context.asset_lookup is None:
            raise ConversionError(f"Registry asset {asset_id!r} is unavailable for {context.slide_id}/{context.element_id}.")
        try:
            result = context.asset_lookup(asset_id)
        except ConversionError:
            raise
        except Exception as exc:  # pragma: no cover - defensive wrapper for registry loaders
            raise ConversionError(f"Registry asset {asset_id!r} cannot be resolved for {context.slide_id}/{context.element_id}: {exc}") from exc
        context.records.append({
            "slideId": context.slide_id, "elementId": context.element_id,
            "kind": "embedded-local-asset", "source": f"asset:{asset_id}",
            "resolvedMimeType": result.split(";", 1)[0].removeprefix("data:") if result.startswith("data:") else "registry-asset",
            "result": "data-uri", "contentChanged": False,
        })
        return result

    resolved: Path
    if original.lower().startswith("file:"):
        resolved = _path_from_file_uri(original)
    elif _is_windows_path(original) or os.path.isabs(original):
        resolved = Path(unquote(original))
    else:
        parsed = urlparse(original)
        if parsed.scheme:
            return original
        resolved = context.source_root / unquote(parsed.path)
    if not resolved.is_file():
        redacted = _redacted_source(original, resolved, context.source_root)
        raise ConversionError(
            f"Local resource is missing for slideId={context.slide_id!r}, elementId={context.element_id!r}: "
            f"{redacted}. Resolve the path relative to {context.source_html_path.name} or provide a registry asset."
        )
    data, mime = _data_uri(resolved)
    context.records.append({
        "slideId": context.slide_id, "elementId": context.element_id,
        "kind": "embedded-local-asset", "source": _redacted_source(original, resolved, context.source_root),
        "resolvedMimeType": mime, "result": "data-uri", "contentChanged": False,
    })
    return data


def _css_url_ranges(value: str) -> Iterable[tuple[int, int, str, str]]:
    """Yield URL token ranges while respecting quotes and parentheses in data URIs."""

    cursor = 0
    while True:
        match = re.search(r"url\s*\(", value[cursor:], flags=re.IGNORECASE)
        if not match:
            return
        start = cursor + match.start()
        content_start = cursor + match.end()
        index = content_start
        depth = 1
        quote: str | None = None
        while index < len(value) and depth:
            char = value[index]
            if quote:
                if char == "\\":
                    index += 2
                    continue
                if char == quote:
                    quote = None
            elif char in {"'", '"'}:
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            index += 1
        if depth:
            return
        raw = value[content_start:index - 1].strip()
        quote = raw[:1] if raw[:1] in {"'", '"'} and raw[-1:] == raw[:1] else ""
        resource = raw[1:-1] if quote else raw
        yield start, index, resource, quote
        cursor = index


def replace_css_urls(value: str, *, context: ResourceContext) -> str:
    parts: list[str] = []
    cursor = 0
    for start, end, resource, quote in _css_url_ranges(value):
        parts.append(value[cursor:start])
        resolved = resolve_embedded_resource(resource, context=context)
        delimiter = quote or '"'
        parts.append(f"url({delimiter}{resolved}{delimiter})")
        cursor = end
    parts.append(value[cursor:])
    return "".join(parts)


class _ResourceEmbeddingParser(HTMLParser):
    """HTML parser that safely rewrites resource-bearing attributes and CSS URLs."""

    _void = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    _always_resource_attrs = {"src", "poster", "data"}
    _href_resource_tags = {"image", "use", "feimage", "object"}

    def __init__(self, context: ResourceContext) -> None:
        super().__init__(convert_charrefs=False)
        self.context = context
        self.parts: list[str] = []
        self.suppressed = 0
        self.stack: list[tuple[str, str]] = []

    def _raw_tag_name(self, fallback: str) -> str:
        match = re.match(r"<\s*([^\s/>]+)", self.get_starttag_text() or "")
        return match.group(1) if match else fallback

    def _attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        values: list[str] = []
        for name, raw in attrs:
            lower = name.lower()
            if lower.startswith("on"):
                continue
            value = raw or ""
            if lower == "style":
                value = replace_css_urls(value, context=self.context)
            elif lower in self._always_resource_attrs or (lower in {"href", "xlink:href"} and tag.lower() in self._href_resource_tags):
                if value.lower().startswith("javascript:"):
                    continue
                value = resolve_embedded_resource(value, context=self.context)
            values.append(f' {name}="{html.escape(value, quote=True)}"')
        return "".join(values)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "script":
            self.suppressed += 1
            return
        if self.suppressed:
            return
        raw_tag = self._raw_tag_name(tag)
        self.parts.append(f"<{raw_tag}{self._attrs(tag, attrs)}>")
        if tag.lower() not in self._void:
            self.stack.append((tag.lower(), raw_tag))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.suppressed:
            return
        raw_tag = self._raw_tag_name(tag)
        self.parts.append(f"<{raw_tag}{self._attrs(tag, attrs)}/>")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script":
            self.suppressed = max(0, self.suppressed - 1)
            return
        if self.suppressed:
            return
        raw_tag = tag
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag.lower():
                raw_tag = self.stack[index][1]
                del self.stack[index:]
                break
        self.parts.append(f"</{raw_tag}>")

    def handle_data(self, data: str) -> None:
        if self.suppressed:
            return
        in_style = bool(self.stack and self.stack[-1][0] == "style")
        self.parts.append(replace_css_urls(data, context=self.context) if in_style else data)

    def handle_comment(self, data: str) -> None:
        if not self.suppressed:
            self.parts.append(f"<!--{data}-->")

    def handle_entityref(self, name: str) -> None:
        if not self.suppressed:
            self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if not self.suppressed:
            self.parts.append(f"&#{name};")


def embed_markup_resources(markup: str, *, context: ResourceContext) -> str:
    parser = _ResourceEmbeddingParser(context)
    parser.feed(markup)
    parser.close()
    return "".join(parser.parts)


class _ResourceScanParser(HTMLParser):
    _always_resource_attrs = {"src", "poster", "data"}
    _href_resource_tags = {"image", "use", "feimage", "object"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.references: list[str] = []
        self._style_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._style_depth += tag.lower() == "style"
        for name, value in attrs:
            lower = name.lower()
            if (lower in self._always_resource_attrs or (lower in {"href", "xlink:href"} and tag.lower() in self._href_resource_tags)) and value:
                self.references.append(value)
            if lower == "style" and value:
                self.references.extend(resource for _, _, resource, _ in _css_url_ranges(value))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self._style_depth -= tag.lower() == "style"

    def handle_endtag(self, tag: str) -> None:
        self._style_depth = max(0, self._style_depth - (tag.lower() == "style"))

    def handle_data(self, data: str) -> None:
        if self._style_depth:
            self.references.extend(resource for _, _, resource, _ in _css_url_ranges(data))


def unresolved_markup_resources(markup: str) -> list[str]:
    parser = _ResourceScanParser()
    parser.feed(markup)
    parser.close()
    return [value for value in parser.references if _is_local_resource(value)]


def scan_document_resources(document: dict[str, object]) -> dict[str, object]:
    """Scan structured resource fields, not prose text, for leaked local URLs."""

    unresolved: list[dict[str, str]] = []
    scanned = 0
    for slide in document.get("slides", []):
        if not isinstance(slide, dict):
            continue
        for element in slide.get("elements", []):
            if not isinstance(element, dict):
                continue
            slide_id, element_id = str(slide.get("id")), str(element.get("id"))
            for field in ("src", "poster", "data"):
                value = element.get(field)
                if isinstance(value, str):
                    scanned += 1
                    if _is_local_resource(value):
                        unresolved.append({"slideId": slide_id, "elementId": element_id, "field": field, "value": "$LOCAL_RESOURCE"})
            markup = element.get("markup")
            if isinstance(markup, str):
                for value in unresolved_markup_resources(markup):
                    scanned += 1
                    unresolved.append({"slideId": slide_id, "elementId": element_id, "field": "markup", "value": "$LOCAL_RESOURCE"})
    return {
        "format": "bento/resource-scan/v1",
        "passed": not unresolved,
        "scannedResourceFields": scanned,
        "unresolved": unresolved,
    }
