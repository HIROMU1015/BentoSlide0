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
from typing import Any, Callable, Iterable
from urllib.parse import unquote, urlparse

from .errors import ConversionError


ResourceLookup = Callable[[str], str]


class ResourceResolutionError(ConversionError):
    """A resource-bearing field cannot be made portable."""


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


def resolve_embedded_resource(value: str, *, context: ResourceContext, field: str | None = None) -> str:
    """Return a portable resource URL and record local-resource embedding."""

    original = html.unescape(value).strip()
    if not original or original.startswith(("data:", "#", "http:", "https:", "mailto:", "tel:")):
        return original
    if original.lower().startswith("javascript:"):
        raise ResourceResolutionError(f"Unsafe javascript resource reference in {context.slide_id}/{context.element_id}.")
    if original.startswith("asset:"):
        asset_reference = original[len("asset:"):]
        asset_id, separator, fragment = asset_reference.partition("#")
        if not asset_id or context.asset_lookup is None:
            raise ResourceResolutionError(f"Registry asset {asset_id!r} is unavailable for {context.slide_id}/{context.element_id}.")
        try:
            result = context.asset_lookup(asset_id)
        except ConversionError as exc:
            raise ResourceResolutionError(str(exc)) from exc
        except Exception as exc:  # pragma: no cover - defensive wrapper for registry loaders
            raise ResourceResolutionError(f"Registry asset {asset_id!r} cannot be resolved for {context.slide_id}/{context.element_id}: {exc}") from exc
        record = {
            "slideId": context.slide_id, "elementId": context.element_id,
            "kind": "embedded-local-asset", "source": f"asset:{asset_id}",
            "resolvedMimeType": result.split(";", 1)[0].removeprefix("data:") if result.startswith("data:") else "registry-asset",
            "result": "data-uri", "contentChanged": False,
        }
        if field:
            record["resourceField"] = field
        if separator:
            record["fragmentPreserved"] = True
        context.records.append(record)
        return result + (f"#{fragment}" if separator else "")

    resolved: Path
    parsed = urlparse(original)
    fragment = parsed.fragment
    if original.lower().startswith("file:"):
        resolved = _path_from_file_uri(original)
    elif _is_windows_path(original) or os.path.isabs(original):
        path_value = original[: -(len(fragment) + 1)] if fragment else original
        resolved = Path(unquote(path_value))
    else:
        if parsed.scheme:
            return original
        resolved = context.source_root / unquote(parsed.path)
    if not resolved.is_file():
        redacted = _redacted_source(original, resolved, context.source_root)
        raise ResourceResolutionError(
            f"Local resource is missing for slideId={context.slide_id!r}, elementId={context.element_id!r}: "
            f"{redacted}. Resolve the path relative to {context.source_html_path.name} or provide a registry asset."
        )
    data, mime = _data_uri(resolved)
    record = {
        "slideId": context.slide_id, "elementId": context.element_id,
        "kind": "embedded-local-asset", "source": _redacted_source(original, resolved, context.source_root),
        "resolvedMimeType": mime, "result": "data-uri", "contentChanged": False,
    }
    if field:
        record["resourceField"] = field
    if fragment:
        record["fragmentPreserved"] = True
    context.records.append(record)
    return data + (f"#{fragment}" if fragment else "")


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
        resolved = resolve_embedded_resource(resource, context=context, field="css-url")
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
                value = resolve_embedded_resource(value, context=self.context, field=lower)
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


RESOURCE_EXTENSIONS = {
    ".avif", ".bmp", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp",
    ".aac", ".flac", ".m4a", ".mp3", ".ogg", ".wav",
    ".m4v", ".mov", ".mp4", ".ogv", ".webm",
    ".woff", ".woff2", ".ttf", ".otf",
}


def _looks_relative_resource(value: str) -> bool:
    parsed = urlparse(value.strip())
    suffix = Path(unquote(parsed.path)).suffix.lower()
    return suffix in RESOURCE_EXTENSIONS


def _looks_explicit_local(value: str) -> bool:
    normalized = html.unescape(value).strip()
    return normalized.lower().startswith("file:") or _is_windows_path(normalized) or normalized.startswith("/")


def embed_chart_option_resources(value: Any, *, context: ResourceContext) -> Any:
    """Recursively embed resource-bearing strings inside a chart option."""

    if isinstance(value, dict):
        return {key: embed_chart_option_resources(item, context=context) for key, item in value.items()}
    if isinstance(value, list):
        return [embed_chart_option_resources(item, context=context) for item in value]
    if not isinstance(value, str):
        return value
    if value.startswith("image://"):
        resource = value[len("image://"):]
        if _is_local_resource(resource):
            return "image://" + resolve_embedded_resource(resource, context=context, field="chart-option")
        return value
    if any(True for _ in _css_url_ranges(value)):
        return replace_css_urls(value, context=context)
    if _looks_explicit_local(value) or (_is_local_resource(value) and _looks_relative_resource(value)):
        return resolve_embedded_resource(value, context=context, field="chart-option")
    return value


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
    """Recursively scan the final Bento document without treating prose as paths."""

    unresolved: list[dict[str, str]] = []
    by_category: dict[str, int] = {}
    scanned = 0
    embedded = 0
    document_assets = document.get("assets", {}) if isinstance(document.get("assets"), dict) else {}

    def record(
        value: str, *, category: str, field: str, slide_id: str = "<document>",
        element_id: str = "<none>", require_embedded: bool = False, asset_reference: bool = False,
    ) -> None:
        nonlocal scanned, embedded
        scanned += 1
        by_category[category] = by_category.get(category, 0) + 1
        normalized = html.unescape(value).strip()
        if asset_reference and normalized in document_assets:
            return
        if normalized.startswith("data:"):
            embedded += 1
            return
        safe_external = normalized.startswith(("#", "http:", "https:", "mailto:", "tel:"))
        if safe_external and not require_embedded:
            return
        local = _is_local_resource(normalized)
        if require_embedded or local:
            unresolved.append({
                "slideId": slide_id, "elementId": element_id, "field": field,
                "value": "$NON_SELF_CONTAINED_RESOURCE" if require_embedded and not local else "$LOCAL_RESOURCE",
            })

    def scan_markup(markup: str, *, slide_id: str, element_id: str, field: str) -> None:
        parser = _ResourceScanParser()
        parser.feed(markup)
        parser.close()
        for reference in parser.references:
            record(reference, category="svgMarkup", field=field, slide_id=slide_id, element_id=element_id)

    def scan_chart(value: Any, *, slide_id: str, element_id: str, field: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                scan_chart(item, slide_id=slide_id, element_id=element_id, field=f"{field}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                scan_chart(item, slide_id=slide_id, element_id=element_id, field=f"{field}[{index}]")
        elif isinstance(value, str):
            if value.startswith("image://"):
                record(value[len("image://"):], category="chartOption", field=field, slide_id=slide_id, element_id=element_id)
                return
            css_references = [resource for _, _, resource, _ in _css_url_ranges(value)]
            if css_references:
                for reference in css_references:
                    record(reference, category="chartOption", field=field, slide_id=slide_id, element_id=element_id)
            elif _looks_explicit_local(value) or (_is_local_resource(value) and _looks_relative_resource(value)):
                record(value, category="chartOption", field=field, slide_id=slide_id, element_id=element_id)

    direct_resource_fields = {"src", "poster", "data", "href", "xlink:href"}
    css_resource_fields = {
        "background", "backgroundImage", "background-image", "mask", "maskImage", "mask-image",
        "filter", "clipPath", "clip-path", "fill", "stroke", "content",
    }

    def walk(value: Any, *, path: tuple[str, ...], slide_id: str = "<document>", element_id: str = "<none>") -> None:
        if isinstance(value, dict):
            current_slide = str(value.get("id")) if path and path[-1] == "slides[]" and isinstance(value.get("id"), str) else slide_id
            current_element = str(value.get("id")) if path and path[-1] == "elements[]" and isinstance(value.get("id"), str) else element_id
            element_type = value.get("type") if path and path[-1] == "elements[]" else None
            for key, item in value.items():
                field_path = ".".join((*path, str(key)))
                if path == () and key == "collab":
                    # Collaboration/session metadata is never a document
                    # resource. In particular, runtime sync tokens may contain
                    # slash-like text that must not be reported as a file path.
                    continue
                if path == () and key == "assets" and isinstance(item, dict):
                    for asset_id, asset_value in item.items():
                        if isinstance(asset_value, str):
                            record(asset_value, category="assets", field=f"assets.{asset_id}", require_embedded=True)
                    continue
                if key == "markup" and isinstance(item, str):
                    scan_markup(item, slide_id=current_slide, element_id=current_element, field="markup")
                    continue
                if key == "option" and element_type == "chart":
                    scan_chart(item, slide_id=current_slide, element_id=current_element, field="option")
                    continue
                if isinstance(item, str) and key == "asset" and element_type == "svg":
                    record(item, category="svgAsset", field="asset", slide_id=current_slide, element_id=current_element, asset_reference=True)
                    continue
                if isinstance(item, str) and key in direct_resource_fields:
                    category = "mediaPoster" if key == "poster" else "mediaSrc" if element_type == "media" else "imageSrc"
                    record(item, category=category, field=key, slide_id=current_slide, element_id=current_element)
                    continue
                if isinstance(item, str) and key in css_resource_fields:
                    refs = [resource for _, _, resource, _ in _css_url_ranges(item)]
                    if refs:
                        for reference in refs:
                            record(reference, category="theme" if path[:1] == ("theme",) else "nested", field=field_path, slide_id=current_slide, element_id=current_element)
                    elif _looks_explicit_local(item) or (_is_local_resource(item) and _looks_relative_resource(item)):
                        record(item, category="theme" if path[:1] == ("theme",) else "nested", field=field_path, slide_id=current_slide, element_id=current_element)
                    continue
                walk(item, path=(*path, str(key)), slide_id=current_slide, element_id=current_element)
        elif isinstance(value, list):
            next_path = ("slides[]",) if path == ("slides",) else (*path[:-1], "elements[]") if path and path[-1] == "elements" else (*path, "[]")
            for item in value:
                walk(item, path=next_path, slide_id=slide_id, element_id=element_id)
        elif isinstance(value, str):
            field_path = ".".join(path)
            prose_field = bool(path and path[-1] in {"html", "notes", "title", "text", "latexSource"})
            if value.startswith("image://"):
                record(value[len("image://"):], category="nested", field=field_path, slide_id=slide_id, element_id=element_id)
            else:
                refs = [resource for _, _, resource, _ in _css_url_ranges(value)]
                if refs:
                    for reference in refs:
                        record(reference, category="nested", field=field_path, slide_id=slide_id, element_id=element_id)
                elif _looks_explicit_local(value) or (not prose_field and _is_local_resource(value) and _looks_relative_resource(value)):
                    record(value, category="nested", field=field_path, slide_id=slide_id, element_id=element_id)

    walk(document, path=())
    return {
        "format": "bento/resource-scan/v2",
        "passed": not unresolved,
        "scannedFields": scanned,
        "scannedResourceFields": scanned,
        "embeddedResources": embedded,
        "unresolved": unresolved,
        "byCategory": dict(sorted(by_category.items())),
    }
