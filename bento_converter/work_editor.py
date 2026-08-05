"""Localhost-only HTTP bridge for editing and saving a final Bento deck."""

from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from .errors import BentoConverterError, ValidationError
from .authoring_storage import AuthoringArtifactStorage, AuthoringConflict
from .html_document import load_html
from .work_editor_storage import WorkEditorConflict, WorkEditorStorage


TOOLBAR = r"""
<script id="bento-work-editor-loader">
(() => {
  const loader = document.currentScript;
  loader.remove();
  const start = () => {
    if (!window.bento || typeof window.bento.serialize !== 'function') return false;
    const host = document.createElement('div');
    host.id = 'bento-work-editor-host';
    host.dataset.workEditorUi = 'true';
    host.innerHTML = `
      <style id="bento-work-editor-style">
      #bento-work-editor{position:fixed;right:12px;top:12px;z-index:2147483647;display:flex;gap:6px;align-items:center;padding:8px;background:rgba(15,23,42,.94);color:#fff;border-radius:8px;box-shadow:0 4px 18px #0004;font:12px/1.3 system-ui,sans-serif}
      #bento-work-editor button{border:0;border-radius:5px;padding:6px 9px;cursor:pointer;background:#e2e8f0;color:#0f172a}
      #bento-work-editor-status{max-width:320px;white-space:pre-wrap}
      </style>
      <div id="bento-work-editor" data-work-editor-ui="true">
        <button id="work-save" type="button">&#x30ED;&#x30FC;&#x30AB;&#x30EB;&#x4FDD;&#x5B58;</button>
        <button id="work-save-validate" type="button">&#x4FDD;&#x5B58;&#x3057;&#x3066;&#x691C;&#x8A3C;</button>
        <button id="work-revert" type="button">&#x4E00;&#x3064;&#x524D;&#x306B;&#x623B;&#x3059;</button>
        <button id="work-reload" type="button">&#x72B6;&#x614B;&#x3092;&#x518D;&#x8AAD;&#x307F;&#x8FBC;&#x307F;</button>
        <span id="bento-work-editor-mode"></span>
        <span id="bento-work-editor-status">&#x63A5;&#x7D9A;&#x4E2D;&hellip;</span>
      </div>`;
    document.body.appendChild(host);

    let revision = null;
    let registryRevision = null;
    let editingMode = 'finalization';
    const status = document.getElementById('bento-work-editor-status');
    const controls = Array.from(host.querySelectorAll('button'));
    controls.forEach(button => { button.disabled = true; });
    const message = text => { status.textContent = text; };
    const errorText = payload => (payload.errors || [payload.error || '\u4e0d\u660e\u306a\u30a8\u30e9\u30fc']).join('\n');
    const originalSerialize = window.bento.serialize.bind(window.bento);
    const guardedSerialize = (...args) => {
      const parent = host.parentNode;
      if (!parent) return originalSerialize(...args);
      const next = host.nextSibling;
      host.remove();
      try { return originalSerialize(...args); }
      finally { parent.insertBefore(host, next); }
    };
    guardedSerialize.workEditorGuard = true;
    window.bento.serialize = guardedSerialize;

    async function refreshStatus() {
      const response = await fetch('/api/status', {cache:'no-store'});
      const payload = await response.json();
      editingMode = payload.editingMode || 'finalization';
      revision = payload.documentRevision || payload.revision;
      registryRevision = payload.registryRevision || null;
      document.getElementById('bento-work-editor-mode').textContent = editingMode;
      message(`revision ${revision.slice(0, 19)}... / validation ${payload.validation}`);
      controls.forEach(button => { button.disabled = false; });
    }
    async function post(path, payload) {
      const response = await fetch(path, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      const result = await response.json();
      if (!response.ok) {
        if (response.status === 409) message('\u5225\u306e\u7de8\u96c6\u304c\u4fdd\u5b58\u3055\u308c\u3066\u3044\u307e\u3059\u3002\n\u6700\u65b0\u72b6\u614b\u3092\u518d\u8aad\u307f\u8fbc\u307f\u3057\u3066\u304f\u3060\u3055\u3044\u3002');
        else message(`\u691c\u8a3c\u307e\u305f\u306f\u4fdd\u5b58\u306b\u5931\u6557\u3057\u307e\u3057\u305f\u3002\n${errorText(result)}`);
        return null;
      }
      if (result.documentRevision || result.revision) revision = result.documentRevision || result.revision;
      if (result.registryRevision) registryRevision = result.registryRevision;
      return result;
    }
    async function serialized() {
      if (!window.bento || typeof window.bento.serialize !== 'function') throw new Error('Bento UI\u304c\u307e\u3060\u8d77\u52d5\u3057\u3066\u3044\u307e\u305b\u3093\u3002');
      return window.bento.serialize();
    }
    const savePayload = html => editingMode === 'authoring'
      ? {baseDocumentRevision:revision,baseRegistryRevision:registryRevision,serializedHtml:html}
      : {baseRevision:revision,serializedHtml:html};
    const revertPayload = () => editingMode === 'authoring'
      ? {baseDocumentRevision:revision,baseRegistryRevision:registryRevision}
      : {baseRevision:revision};
    document.getElementById('work-save').addEventListener('click', async () => {
      try { const result = await post('/api/save', savePayload(await serialized())); if (result) message(`\u4fdd\u5b58\u3057\u307e\u3057\u305f\uff1arevision ${result.documentRevision || result.revision}`); }
      catch (error) { message(error.message); }
    });
    document.getElementById('work-save-validate').addEventListener('click', async () => {
      try {
        const html = await serialized();
        const checked = await post('/api/validate', savePayload(html));
        if (!checked) return;
        const result = await post('/api/save', savePayload(html));
        if (result) message(`\u4fdd\u5b58\u30fb\u691c\u8a3c\u3057\u307e\u3057\u305f\u3002revision ${result.documentRevision || result.revision}`);
      } catch (error) { message(error.message); }
    });
    document.getElementById('work-revert').addEventListener('click', async () => {
      const result = await post('/api/revert', revertPayload());
      if (result) location.reload();
    });
    document.getElementById('work-reload').addEventListener('click', () => location.reload());
    refreshStatus().catch(error => message(error.message));
    return true;
  };
  const timer = setInterval(() => {
    if (start()) clearInterval(timer);
  }, 25);
})();
</script>
"""


def inject_work_toolbar(html: str) -> str:
    marker = "</body>"
    index = html.lower().rfind(marker)
    return html[:index] + TOOLBAR + html[index:] if index >= 0 else html + TOOLBAR


class WorkEditorHTTPServer(ThreadingHTTPServer):
    storage: WorkEditorStorage | AuthoringArtifactStorage

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            self.storage.release_writer_lease()


def create_work_editor_server(
    storage: WorkEditorStorage | AuthoringArtifactStorage, *, host: str = "127.0.0.1", port: int = 8765,
) -> WorkEditorHTTPServer:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise BentoConverterError("Work editor may bind only to a loopback address.")

    storage.acquire_writer_lease()

    class Handler(BaseHTTPRequestHandler):
        server: WorkEditorHTTPServer

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            body = (json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, payload: str) -> None:
            body = payload.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _request_json(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise BentoConverterError("Invalid Content-Length header.") from exc
            if length <= 0 or length > 64 * 1024 * 1024:
                raise BentoConverterError("Request body must be between 1 byte and 64 MiB.")
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BentoConverterError(f"Invalid JSON request body: {exc}") from exc
            if not isinstance(value, dict):
                raise BentoConverterError("Request JSON root must be an object.")
            return value

        def _error(self, exc: Exception) -> None:
            if isinstance(exc, (WorkEditorConflict, AuthoringConflict)):
                self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
            elif isinstance(exc, ValidationError):
                self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc), "errors": list(exc.issues)})
            elif isinstance(exc, BentoConverterError):
                self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
            else:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"Work editor internal error: {exc}"})

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            try:
                if path == "/":
                    self._html(inject_work_toolbar(load_html(self.server.storage.target)))
                elif path == "/api/status":
                    self._json(HTTPStatus.OK, self.server.storage.status())
                elif path == "/api/document":
                    self._json(HTTPStatus.OK, self.server.storage.document_response())
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            except Exception as exc:  # pragma: no cover - defensive HTTP boundary
                self._error(exc)

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            try:
                payload = self._request_json()
                if path in {"/api/save", "/api/validate"}:
                    serialized_html = payload.get("serializedHtml")
                    if not isinstance(serialized_html, str):
                        raise BentoConverterError("serializedHtml must be a string returned by window.bento.serialize().")
                    if getattr(self.server.storage, "editing_mode", "finalization") == "authoring":
                        arguments = {
                            "base_document_revision": str(payload.get("baseDocumentRevision", "")),
                            "base_registry_revision": str(payload.get("baseRegistryRevision", "")),
                            "registry": payload.get("registry"),
                        }
                        if arguments["registry"] is not None and not isinstance(arguments["registry"], dict):
                            raise BentoConverterError("registry must be an object when provided")
                        result = self.server.storage.save_serialized(serialized_html, **arguments) if path == "/api/save" else self.server.storage.validate_serialized(serialized_html, **arguments)
                    else:
                        result = self.server.storage.save_serialized(
                            serialized_html, base_revision=str(payload.get("baseRevision", "")),
                        ) if path == "/api/save" else self.server.storage.validate_serialized(serialized_html)
                elif path == "/api/revert":
                    if getattr(self.server.storage, "editing_mode", "finalization") == "authoring":
                        result = self.server.storage.revert(
                            base_document_revision=str(payload.get("baseDocumentRevision", "")),
                            base_registry_revision=str(payload.get("baseRegistryRevision", "")),
                        )
                    else:
                        result = self.server.storage.revert(base_revision=str(payload.get("baseRevision", "")))
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                    return
                self._json(HTTPStatus.OK, result)
            except Exception as exc:
                self._error(exc)

    try:
        server = WorkEditorHTTPServer((host, port), Handler)
        server.storage = storage
        return server
    except BaseException:
        storage.release_writer_lease()
        raise
