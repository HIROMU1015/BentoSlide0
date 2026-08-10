"""Serve single-deck or modular HTML authoring sources on loopback."""

from __future__ import annotations

import argparse
import html
import json
import mimetypes
import socket
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from scripts.deck_workflow import WorkflowError, load_state, repository_root


STATUS_FORMAT = "bento/html-preview-status/v1"
ACTIVE_HTML_CHANGE_STATUSES = {"proposed", "approved"}


def _html_change_preview(state: dict[str, Any]) -> dict[str, Any] | None:
    proposal = state.get("authoring", {}).get("htmlChange")
    if not isinstance(proposal, dict) or proposal.get("status") not in ACTIVE_HTML_CHANGE_STATUSES:
        return None
    candidate = str(proposal.get("candidateHtml") or "")
    return {
        "proposalId": proposal.get("proposalId"),
        "status": proposal.get("status"),
        "scope": proposal.get("scope"),
        "summary": proposal.get("summary"),
        "impactSummary": proposal.get("impactSummary"),
        "affectedSlideIds": list(proposal.get("affectedSlideIds") or []),
        "slideTitles": dict(proposal.get("slideTitles") or {}),
        "candidatePath": candidate,
        "candidateUrl": "/" + quote(candidate.replace("\\", "/"), safe="/"),
    }


def _preview_snapshot(repository: Path) -> tuple[dict[str, Any], list[str], str | None]:
    state = load_state(repository)
    if state.get("schemaVersion") == 2 and state["authoring"]["mode"] in {"single", "imported"}:
        relative = state["authoring"]["entryHtml"]
        path = (repository / relative).resolve()
        files = [relative] if path.is_file() else []
        return state, files, relative if files else None
    chapters_root = (repository / "chapters").resolve()
    files = [path.relative_to(repository).as_posix() for path in sorted(chapters_root.glob("*.preview.html")) if path.is_file()]
    current_id = state["workflow"].get("currentChapter")
    current_path = None
    if current_id and current_id in state["chapters"]:
        candidate = state["chapters"][current_id]["html"]
        if candidate in files:
            current_path = candidate
    return state, files, current_path


def _index_html(repository: Path) -> bytes:
    state, files, current_path = _preview_snapshot(repository)
    stage = html.escape(state["workflow"]["stage"])
    single = state.get("schemaVersion") == 2 and state["authoring"]["mode"] in {"single", "imported"}
    if single:
        current_section = html.escape(state["workflow"].get("currentSection") or "-")
        sections = []
        slides = []
        for section_id, section in state["sections"].items():
            section_label = html.escape(section["title"])
            section_hash = quote(section_id, safe="")
            marker = " <strong>current</strong>" if section_id == state["workflow"].get("currentSection") else ""
            sections.append(f'<li><a href="#section={section_hash}">{section_label}</a>{marker}</li>')
            for slide_id in section["slideIds"]:
                slides.append(f'<li><a href="#slide={quote(slide_id, safe="")}">{html.escape(slide_id)}</a></li>')
        source_url = "/" + quote((current_path or "").replace("\\", "/"), safe="/")
        change = _html_change_preview(state)
        change_panel = ""
        if change:
            affected = "".join(
                f"<li>{html.escape(str(change['slideTitles'].get(slide_id, slide_id)))}</li>"
                for slide_id in change["affectedSlideIds"]
            )
            change_panel = f"""
<section class="change-review" id="html-change-review">
<h2>変更案の確認</h2>
<p><code>{html.escape(str(change['status']))}</code> / <code>{html.escape(str(change['scope']))}</code></p>
<p>{html.escape(str(change['summary']))}</p>
<p>{html.escape(str(change['impactSummary']))}</p>
<details><summary>確認が必要なスライド ({len(change['affectedSlideIds'])})</summary><ul>{affected}</ul></details>
<p><button id="show-candidate" type="button" data-url="{html.escape(change['candidateUrl'], quote=True)}">変更案を見る</button>
<button id="show-canonical" type="button" data-url="{html.escape(source_url, quote=True)}">現在版へ戻る</button></p>
</section>"""
        payload = f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>BentoSlide deck preview</title>
<style>html,body{{height:100%;margin:0}}body{{font:14px/1.5 system-ui,sans-serif;color:#172033;display:grid;grid-template-columns:300px 1fr}}aside{{padding:18px;overflow:auto;border-right:1px solid #ccd5e1;background:#f8fafc}}iframe{{width:100%;height:100%;border:0;background:white}}code{{background:#e7edf5;padding:2px 5px;border-radius:4px}}li{{margin:5px 0}}button{{padding:6px 10px}}strong{{color:#1d4ed8}}.change-review{{margin:16px -6px;padding:12px;border:1px solid #93c5fd;border-radius:8px;background:#eff6ff}}.change-review h2{{margin-top:0}}</style>
</head><body><aside><h1>BentoSlide</h1><p>stage: <code>{stage}</code><br>current section: <code>{current_section}</code></p>
<button id="reload" type="button">Reload</button>{change_panel}<h2>Sections</h2><ul>{''.join(sections) or '<li>No sections</li>'}</ul>
<h2>Slides</h2><ul>{''.join(slides) or '<li>No registered slides</li>'}</ul></aside>
<iframe id="deck" title="Deck preview" src="{source_url}"></iframe>
<script>const frame=document.getElementById('deck');function navigate(){{const match=location.hash.match(/^#(section|slide)=(.+)$/);if(!match)return;const attr=match[1]==='section'?'data-section-id':'data-slide-id';const apply=()=>{{const node=frame.contentDocument&&frame.contentDocument.querySelector('['+attr+'="'+CSS.escape(decodeURIComponent(match[2]))+'"]');if(node)node.scrollIntoView({{block:'start'}});}};if(frame.contentDocument)apply();else frame.addEventListener('load',apply,{{once:true}});}}window.addEventListener('hashchange',navigate);frame.addEventListener('load',navigate);document.getElementById('reload').onclick=()=>{{frame.contentWindow.location.reload();}};for(const id of ['show-candidate','show-canonical']){{const button=document.getElementById(id);if(button)button.onclick=()=>{{frame.src=button.dataset.url;}};}}navigate();</script>
</body></html>"""
        return payload.encode("utf-8")
    current_id = html.escape(state["workflow"].get("currentChapter") or "-")
    items = []
    for relative in files:
        label = html.escape(Path(relative).name)
        href = "/" + quote(relative.replace("\\", "/"), safe="/")
        marker = " <strong>current</strong>" if relative == current_path else ""
        items.append(f'<li><a href="{href}">{label}</a>{marker}</li>')
    if not items:
        items.append("<li>No chapter preview HTML exists yet.</li>")
    payload = f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>BentoSlide chapter preview</title>
<style>body{{font:16px/1.6 system-ui,sans-serif;max-width:860px;margin:48px auto;padding:0 24px;color:#172033}}code{{background:#eef2f7;padding:2px 6px;border-radius:5px}}li{{margin:10px 0}}strong{{color:#1d4ed8}}</style>
</head><body><h1>BentoSlide chapter preview</h1><p>stage: <code>{stage}</code><br>current chapter: <code>{current_id}</code></p><ul>{''.join(items)}</ul><p>After an agent updates a chapter, reload its tab.</p></body></html>"""
    return payload.encode("utf-8")


def _safe_preview_path(repository: Path, request_path: str) -> Path | None:
    decoded = unquote(request_path)
    if "\x00" in decoded or "\\" in decoded:
        return None
    pure = PurePosixPath(decoded.lstrip("/"))
    if not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        return None
    state = load_state(repository)
    if state.get("schemaVersion") == 2 and state["authoring"]["mode"] in {"single", "imported"}:
        entry = (repository / state["authoring"]["entryHtml"]).resolve()
        allowed_root = entry.parent
    else:
        if pure.parts[0] != "chapters":
            return None
        allowed_root = (repository / "chapters").resolve()
    candidate = (repository.joinpath(*pure.parts)).resolve()
    try:
        candidate.relative_to(allowed_root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


class HtmlPreviewServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], repository: Path):
        self.repository = repository.resolve()
        super().__init__(address, HtmlPreviewHandler)

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class HtmlPreviewHandler(BaseHTTPRequestHandler):
    server: HtmlPreviewServer

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        sys.stdout.write(f"{self.address_string()} - {format % args}\n")
        sys.stdout.flush()

    def _send(self, status: HTTPStatus, payload: bytes, content_type: str) -> None:
        self.send_response(status.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _json(self, status: HTTPStatus, value: dict[str, Any]) -> None:
        self._send(status, (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8"), "application/json; charset=utf-8")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        route = urlsplit(self.path).path
        if route == "/":
            self._send(HTTPStatus.OK, _index_html(self.server.repository), "text/html; charset=utf-8")
            return
        if route == "/api/status":
            state, files, current_path = _preview_snapshot(self.server.repository)
            host, port = self.server.server_address[:2]
            single = state.get("schemaVersion") == 2 and state["authoring"]["mode"] in {"single", "imported"}
            self._json(HTTPStatus.OK, {
                "format": STATUS_FORMAT,
                "repository": str(self.server.repository),
                "stage": state["workflow"]["stage"],
                "currentChapter": state["workflow"].get("currentChapter"),
                "currentSection": state["workflow"].get("currentSection") if single else None,
                "currentSlide": (
                    next(iter(state["sections"].get(state["workflow"].get("currentSection"), {}).get("slideIds", [])), None)
                    if single else None
                ),
                "currentPath": current_path,
                "chapters": files,
                "mode": state["authoring"]["mode"] if single else "modular",
                "sections": list(state["sections"]) if single else [],
                "slides": [slide for section in state["sections"].values() for slide in section["slideIds"]] if single else [],
                "htmlChange": _html_change_preview(state) if single else None,
                "url": f"http://{host}:{port}/",
            })
            return
        path = _safe_preview_path(self.server.repository, route)
        if path is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            payload = path.read_bytes()
        except OSError as exc:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json", "image/svg+xml"}:
            content_type += "; charset=utf-8"
        self._send(HTTPStatus.OK, payload, content_type)


def create_preview_server(repository: str | Path, *, host: str = "127.0.0.1", port: int = 4173) -> HtmlPreviewServer:
    if host != "127.0.0.1":
        raise WorkflowError("HTML preview must bind exactly to 127.0.0.1")
    if port < 1 or port > 65535:
        raise WorkflowError(f"Invalid preview port: {port}")
    root = repository_root(repository)
    load_state(root)
    state = load_state(root)
    if state.get("schemaVersion") == 2 and state["authoring"]["mode"] in {"single", "imported"}:
        entry = root / state["authoring"]["entryHtml"]
        if not entry.is_file():
            raise WorkflowError(f"Single HTML authoring entry does not exist: {entry}")
    else:
        chapters = root / "chapters"
        if not chapters.is_dir():
            raise WorkflowError(f"chapters/ does not exist: {chapters}")
    return HtmlPreviewServer((host, port), root)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--root", required=True, type=Path)
    result.add_argument("--host", default="127.0.0.1")
    result.add_argument("--port", type=int, default=4173)
    return result


def run(args: argparse.Namespace) -> int:
    server = create_preview_server(args.root, host=args.host, port=args.port)
    host, port = server.server_address[:2]
    print(f"BentoSlide HTML preview: http://{host}:{port}/")
    print(f"Repository: {server.repository}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run(parser().parse_args(argv))
    except (WorkflowError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
