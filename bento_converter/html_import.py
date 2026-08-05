"""Static, no-script normalization for untrusted HTML deck imports."""

from __future__ import annotations

import hashlib
import html
import os
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

from .errors import BentoConverterError


DANGEROUS_ELEMENTS = {"script", "iframe", "object", "embed", "base"}
VOID_ELEMENTS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
URL_ATTRIBUTES = {
    "src", "href", "xlink:href", "poster", "data", "srcset", "action", "formaction",
    "ping", "cite", "background",
}
ELEMENT_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "span", "img", "svg", "table", "canvas", "video", "audio"}
UNSUPPORTED_CSS = ("filter:", "backdrop-filter:", "clip-path:", "mix-blend-mode:", "perspective:")


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Node | str"] = field(default_factory=list)

    def descendants(self) -> Iterable["Node"]:
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.descendants()


class StaticHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.root = Node("__root__")
        self.stack = [self.root]
        self.removed: list[dict[str, Any]] = []
        self._skip_depth = 0

    def handle_decl(self, _decl: str) -> None:
        return

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._skip_depth:
            if tag not in VOID_ELEMENTS:
                self._skip_depth += 1
            return
        values = {name.lower(): value or "" for name, value in attrs}
        if tag in DANGEROUS_ELEMENTS or (tag == "meta" and values.get("http-equiv", "").lower() == "refresh"):
            self.removed.append({"tag": tag, "reason": "active-or-embedded-content"})
            if tag not in VOID_ELEMENTS:
                self._skip_depth = 1
            return
        node = Node(tag, values)
        self.stack[-1].children.append(node)
        if tag not in VOID_ELEMENTS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if self.stack[-1].tag == tag.lower() and tag.lower() not in VOID_ELEMENTS:
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._skip_depth:
            self._skip_depth -= 1
            return
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.stack[-1].children.append(data)

    def handle_entityref(self, name: str) -> None:
        if not self._skip_depth:
            self.stack[-1].children.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if not self._skip_depth:
            self.stack[-1].children.append(f"&#{name};")


def _serialize(node: Node | str) -> str:
    if isinstance(node, str):
        if node.startswith("&") and node.endswith(";") and re.fullmatch(r"&#?[A-Za-z0-9]+;", node):
            return node
        return html.escape(node, quote=False)
    attributes = "".join(
        f' {name}="{html.escape(value, quote=True)}"' for name, value in sorted(node.attrs.items())
    )
    if node.tag in VOID_ELEMENTS:
        return f"<{node.tag}{attributes}>"
    return f"<{node.tag}{attributes}>" + "".join(_serialize(child) for child in node.children) + f"</{node.tag}>"


def _matches(node: Node, selector: str) -> bool:
    selector = selector.strip()
    if not selector or any(token in selector for token in (",", ">", "+", "~", " ")):
        raise BentoConverterError("Only one simple slide selector is supported (tag, .class, #id, or [attribute])")
    attribute = re.fullmatch(r"\[([A-Za-z_:][-A-Za-z0-9_:.]*)\]", selector)
    if attribute:
        return attribute.group(1).lower() in node.attrs
    identifier = re.fullmatch(r"#([A-Za-z_][-A-Za-z0-9_:.]*)", selector)
    if identifier:
        return node.attrs.get("id") == identifier.group(1)
    match = re.fullmatch(r"([A-Za-z][A-Za-z0-9-]*)?(?:\.([A-Za-z_][-A-Za-z0-9_-]*))?", selector)
    if not match:
        raise BentoConverterError("Unsupported slide selector syntax")
    tag, class_name = match.groups()
    if tag and node.tag != tag.lower():
        return False
    if class_name and class_name not in node.attrs.get("class", "").split():
        return False
    return bool(tag or class_name)


def _all_nodes(root: Node) -> list[Node]:
    return [child for child in root.descendants()]


def _sanitize_css(value: str, report: dict[str, Any], *, location: str) -> str:
    for feature in UNSUPPORTED_CSS:
        if feature in value.lower():
            report["unsupportedCss"].append({"location": location, "feature": feature[:-1]})
    remote_pattern = re.compile(r"url\(\s*(['\"]?)(https?://[^)'\"]+)\1\s*\)", re.IGNORECASE)
    for match in remote_pattern.finditer(value):
        report["remoteResources"].append({"location": location, "url": match.group(2), "action": "disabled"})
    value = remote_pattern.sub('url("")', value)
    forbidden = re.search(r"url\(\s*['\"]?\s*(?:javascript|file):", value, re.IGNORECASE)
    if forbidden:
        raise BentoConverterError(f"Forbidden CSS URL scheme at {location}")
    import_pattern = re.compile(r"@import\s+[^;]+;?", re.IGNORECASE)
    for match in import_pattern.finditer(value):
        report["remoteResources"].append({"location": location, "url": match.group(0), "action": "removed-import"})
    return import_pattern.sub("", value)


def _safe_local_resource(
    raw: str, *, input_path: Path, repository: Path, deck_dir: Path,
    copy_assets: bool, asset_payloads: dict[Path, bytes],
) -> tuple[str, dict[str, Any] | None]:
    split = urlsplit(raw)
    if split.scheme.lower() in {"data", "http", "https"} or raw.startswith("#"):
        return raw, None
    if split.scheme or raw.startswith("//"):
        raise BentoConverterError(f"Unsupported imported resource URL scheme: {raw}")
    source = (input_path.parent / unquote(split.path)).resolve()
    try:
        source.relative_to(repository / "imports")
    except ValueError as exc:
        raise BentoConverterError(f"Imported local resource escapes the isolated imports/ tree: {raw}") from exc
    if not source.is_file():
        raise BentoConverterError(f"Imported local resource does not exist: {raw}")
    if copy_assets:
        digest = hashlib.sha256(source.read_bytes()).hexdigest()[:12]
        destination = deck_dir / "assets/imported" / f"{digest}-{source.name}"
        asset_payloads[destination] = source.read_bytes()
        rewritten = destination.relative_to(deck_dir).as_posix()
    else:
        rewritten = Path(os.path.relpath(source, deck_dir)).as_posix()
    if split.query:
        rewritten += "?" + split.query
    if split.fragment:
        rewritten += "#" + split.fragment
    return rewritten, {"source": source.relative_to(repository).as_posix(), "rewritten": rewritten}


def normalize_imported_html(
    source: str, *, input_path: str | Path, repository: str | Path,
    slide_selector: str | None, width: int, height: int,
    copy_assets: bool, generate_ids: bool,
) -> tuple[str, dict[str, Any], dict[str, bytes], dict[str, Any]]:
    root = Path(repository).resolve()
    original = Path(input_path).resolve()
    imports_root = root / "imports"
    try:
        original.relative_to(imports_root)
    except ValueError as exc:
        raise BentoConverterError("Imported HTML must be isolated under repository imports/") from exc
    parser = StaticHtmlParser()
    parser.feed(source)
    parser.close()
    report: dict[str, Any] = {
        "format": "bento/html-import-report/v1", "input": original.relative_to(root).as_posix(),
        "scriptExecution": "disabled-static-parser", "networkAccess": "disabled-no-fetch",
        "removed": list(parser.removed), "removedEventHandlers": [], "remoteResources": [],
        "localResources": [], "unsupportedCss": [], "fallbackCandidates": [],
        "sizeChecks": [], "generatedSlideIds": [], "generatedElementIds": [],
    }
    nodes = _all_nodes(parser.root)
    selector = slide_selector
    if selector is None:
        contracted = [node for node in nodes if node.tag == "section" and "slide" in node.attrs.get("class", "").split()]
        if contracted:
            selector = "section.slide"
        else:
            raise BentoConverterError("Slide candidates are ambiguous; provide --slide-selector explicitly")
    slides = [node for node in nodes if _matches(node, selector)]
    if not slides:
        raise BentoConverterError(f"Slide selector matched no elements: {selector}")
    slide_set = {id(node) for node in slides}
    if any(id(descendant) in slide_set for slide in slides for descendant in slide.descendants()):
        raise BentoConverterError("Slide selector produced nested slide candidates")

    asset_payloads: dict[Path, bytes] = {}
    deck_dir = root / "deck"

    def sanitize_style(value: str, *, location: str) -> str:
        sanitized = _sanitize_css(value, report, location=location)
        pattern = re.compile(r"url\(\s*(['\"]?)([^)'\"]+)\1\s*\)", re.IGNORECASE)

        def replace(match: re.Match[str]) -> str:
            raw = match.group(2).strip()
            if not raw or raw.startswith(("#", "data:")):
                return match.group(0)
            rewritten, evidence = _safe_local_resource(
                raw, input_path=original, repository=root, deck_dir=deck_dir,
                copy_assets=copy_assets, asset_payloads=asset_payloads,
            )
            if evidence:
                report["localResources"].append({**evidence, "location": location})
            return f'url("{rewritten}")'

        return pattern.sub(replace, sanitized)

    used_slide_ids: set[str] = set()
    used_element_ids: set[str] = set()
    for slide_index, slide in enumerate(slides, start=1):
        slide.tag = "section"
        classes = slide.attrs.get("class", "").split()
        if "slide" not in classes:
            classes.append("slide")
        slide.attrs["class"] = " ".join(classes)
        slide_id = slide.attrs.get("data-slide-id")
        if not slide_id:
            if not generate_ids:
                raise BentoConverterError("Imported slides require data-slide-id or --generate-ids")
            slide_id = f"slide-{slide_index:03d}"
            slide.attrs["data-slide-id"] = slide_id
            report["generatedSlideIds"].append(slide_id)
        if slide_id in used_slide_ids:
            raise BentoConverterError(f"Imported HTML contains duplicate slide ID: {slide_id}")
        used_slide_ids.add(slide_id)
        style = slide.attrs.get("style", "")
        width_match = re.search(r"(?:^|;)\s*width\s*:\s*([0-9.]+)px", style, re.IGNORECASE)
        height_match = re.search(r"(?:^|;)\s*height\s*:\s*([0-9.]+)px", style, re.IGNORECASE)
        actual = [float(width_match.group(1)) if width_match else None, float(height_match.group(1)) if height_match else None]
        report["sizeChecks"].append({"slideId": slide_id, "actual": actual, "normalized": [width, height]})
        style = re.sub(r"(?:^|;)\s*(?:width|height)\s*:\s*[^;]+", "", style, flags=re.IGNORECASE)
        slide.attrs["style"] = sanitize_style(
            style + f";width:{width}px;height:{height}px", location=f"slide:{slide_id}:style",
        ).lstrip(";")
        element_index = 0
        for node in [slide, *slide.descendants()]:
            for name in list(node.attrs):
                if name.startswith("on"):
                    report["removedEventHandlers"].append({"slideId": slide_id, "tag": node.tag, "attribute": name})
                    del node.attrs[name]
            if "style" in node.attrs:
                node.attrs["style"] = sanitize_style(node.attrs["style"], location=f"slide:{slide_id}:{node.tag}:style")
            if node.tag == "canvas":
                report["fallbackCandidates"].append({"slideId": slide_id, "tag": "canvas", "reason": "script-disabled"})
            if node is not slide and node.tag in ELEMENT_TAGS:
                identifier = node.attrs.get("data-bento-id")
                if not identifier and generate_ids:
                    element_index += 1
                    identifier = f"{slide_id}-element-{element_index:03d}"
                    node.attrs["data-bento-id"] = identifier
                    report["generatedElementIds"].append(identifier)
                if identifier:
                    if identifier in used_element_ids:
                        raise BentoConverterError(f"Imported HTML contains duplicate Bento element ID: {identifier}")
                    used_element_ids.add(identifier)

    removable: set[int] = set()
    for node in nodes:
        for name in list(node.attrs):
            if name not in URL_ATTRIBUTES:
                continue
            raw = node.attrs[name].strip()
            lowered = raw.lower()
            if lowered.startswith("javascript:"):
                raise BentoConverterError(f"javascript: URL is forbidden in imported HTML: {raw}")
            if lowered.startswith(("http://", "https://", "//")):
                report["remoteResources"].append({"tag": node.tag, "attribute": name, "url": raw, "action": "disabled"})
                if node.tag == "link":
                    removable.add(id(node))
                else:
                    del node.attrs[name]
                    node.attrs[f"data-import-remote-{name}"] = raw
                continue
            if raw and not raw.startswith(("#", "data:")):
                rewritten, evidence = _safe_local_resource(
                    raw, input_path=original, repository=root, deck_dir=deck_dir,
                    copy_assets=copy_assets, asset_payloads=asset_payloads,
                )
                node.attrs[name] = rewritten
                if evidence:
                    report["localResources"].append(evidence)
        if node.tag == "style":
            node.children = [
                sanitize_style(child, location="style-block") if isinstance(child, str) else child
                for child in node.children
            ]
    for parent in [parser.root, *nodes]:
        parent.children = [child for child in parent.children if not isinstance(child, Node) or id(child) not in removable]

    html_node = next((node for node in parser.root.children if isinstance(node, Node) and node.tag == "html"), None)
    if html_node is None:
        body = Node("body", {}, list(parser.root.children))
        html_node = Node("html", {"lang": "en"}, [Node("head"), body])
    normalized = "<!doctype html>\n" + _serialize(html_node) + "\n"
    registry = {
        "format": "bento/html-registry/v2", "unitId": "deck",
        "sources": {"imported-html": {
            "path": original.relative_to(root).as_posix(), "type": "html", "role": "imported",
        }},
        "document": {"importedFrom": original.relative_to(root).as_posix()},
        "assets": {}, "fonts": {}, "equations": {}, "figures": {}, "tables": {}, "charts": {},
        "protected": {"slideIds": [], "elementIds": [], "requiredText": []},
    }
    report["slideSelector"] = selector
    report["slideIds"] = sorted(used_slide_ids)
    report["assetCopies"] = [path.relative_to(root).as_posix() for path in sorted(asset_payloads)]
    report["status"] = "pass"
    return normalized, registry, {str(path): payload for path, payload in asset_payloads.items()}, report
