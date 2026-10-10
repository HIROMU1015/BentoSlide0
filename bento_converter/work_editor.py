"""Localhost-only HTTP bridge for editing and saving a final Bento deck."""

from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from .errors import BentoConverterError, ValidationError
from .authoring_storage import AuthoringArtifactStorage, AuthoringConflict
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
      #bento-work-editor{position:fixed;left:50%;right:auto;top:52px;transform:translateX(-50%);z-index:2147483647;display:flex;flex-wrap:wrap;max-width:calc(100vw - 24px);gap:4px;align-items:center;padding:5px 6px;box-sizing:border-box;background:rgba(15,23,42,.94);color:#fff;border-radius:6px;box-shadow:0 3px 12px #0004;font:11px/1.2 system-ui,sans-serif}
      #bento-work-editor button{border:0;border-radius:4px;padding:4px 7px;cursor:pointer;background:#e2e8f0;color:#0f172a;font:inherit}
      #bento-work-editor-mode{font-size:10px;opacity:.72}
      #bento-work-editor-status{display:block;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
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
    let baselineDocumentSnapshot = null;
    let reloadInProgress = false;
    let refreshInProgress = false;
    let editingMode = 'finalization';
    const status = document.getElementById('bento-work-editor-status');
    const controls = Array.from(host.querySelectorAll('button'));
    controls.forEach(button => { button.disabled = true; });
    const message = text => { status.textContent = text; status.title = text; };
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

    const markLoadedRevision = value => {
      const url = new URL(location.href);
      url.searchParams.set('revision', value || Date.now().toString());
      history.replaceState(null, '', url.toString());
    };
    const documentSnapshot = () => {
      const document = JSON.parse(JSON.stringify(window.bento.doc));
      delete document.collab;
      delete document.modified;
      return JSON.stringify(document);
    };
    const afterDocumentRender = () => new Promise(resolve => {
      requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(resolve, 100)));
    });
    async function loadLatestDocument({mode = editingMode, announce = true} = {}) {
      const response = await fetch('/api/document', {cache:'no-store'});
      const payload = await response.json();
      if (!response.ok) throw new Error(errorText(payload));
      if (!payload.document || typeof payload.document !== 'object') {
        throw new Error('\u6700\u65b0\u306eBento\u6587\u66f8\u3092\u53d6\u5f97\u3067\u304d\u307e\u305b\u3093\u3067\u3057\u305f\u3002');
      }
      if (typeof window.bento.loadDoc !== 'function' || !window.bento.loadDoc(JSON.stringify(payload.document))) {
        throw new Error('Bento UI\u304c\u6700\u65b0\u306e\u6587\u66f8\u3092\u8aad\u307f\u8fbc\u3081\u307e\u305b\u3093\u3067\u3057\u305f\u3002');
      }
      revision = payload.documentRevision || payload.revision;
      registryRevision = payload.registryRevision || null;
      editingMode = mode || editingMode;
      baselineDocumentSnapshot = documentSnapshot();
      markLoadedRevision(revision);
      await afterDocumentRender();
      document.getElementById('bento-work-editor-mode').textContent = editingMode;
      controls.forEach(button => { button.disabled = false; });
      if (announce) message(`\u6700\u65b0\u72b6\u614b\u3092\u753b\u9762\u306b\u53cd\u6620\u3057\u307e\u3057\u305f\uff1arevision ${revision}`);
      return payload;
    }
    async function refreshStatus({announce = true} = {}) {
      const response = await fetch('/api/revision', {cache:'no-store'});
      const payload = await response.json();
      const nextRevision = payload.documentRevision || payload.revision;
      const nextRegistryRevision = payload.registryRevision || null;
      const changedElsewhere = revision !== null && (
        nextRevision !== revision || nextRegistryRevision !== registryRevision
      );
      if (changedElsewhere) {
        if (reloadInProgress) return;
        const currentDocumentSnapshot = documentSnapshot();
        if (baselineDocumentSnapshot !== null && currentDocumentSnapshot === baselineDocumentSnapshot) {
          reloadInProgress = true;
          controls.forEach(button => { button.disabled = true; });
          message('\u5225\u306e\u5909\u66f4\u3092\u753b\u9762\u306b\u53cd\u6620\u4e2d\u3067\u3059\u2026');
          try { await loadLatestDocument({mode: payload.editingMode || editingMode}); }
          finally {
            reloadInProgress = false;
            controls.forEach(button => { button.disabled = false; });
          }
          return;
        }
        message('\u5225\u306e\u5909\u66f4\u304c\u4fdd\u5b58\u3055\u308c\u307e\u3057\u305f\u3002\n\u672a\u4fdd\u5b58\u306e\u5909\u66f4\u304c\u3042\u308b\u305f\u3081\u3001\u81ea\u52d5\u518d\u8aad\u307f\u8fbc\u307f\u3092\u505c\u6b62\u3057\u3066\u3044\u307e\u3059\u3002\n\u5185\u5bb9\u3092\u78ba\u8a8d\u3057\u3066\u300c\u72b6\u614b\u3092\u518d\u8aad\u307f\u8fbc\u307f\u300d\u3092\u9078\u3093\u3067\u304f\u3060\u3055\u3044\u3002');
        return;
      }
      editingMode = payload.editingMode || 'finalization';
      revision = nextRevision;
      registryRevision = nextRegistryRevision;
      if (baselineDocumentSnapshot === null) baselineDocumentSnapshot = documentSnapshot();
      document.getElementById('bento-work-editor-mode').textContent = editingMode;
      if (announce) message(`revision ${revision.slice(0, 19)}...`);
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
      try { const html = await serialized(); const result = await post('/api/save', savePayload(html)); if (result) { baselineDocumentSnapshot = documentSnapshot(); message(result.noOp ? `\u5909\u66f4\u306f\u3042\u308a\u307e\u305b\u3093\uff1arevision ${result.documentRevision || result.revision}` : `\u4fdd\u5b58\u3057\u307e\u3057\u305f\uff1arevision ${result.documentRevision || result.revision}`); } }
      catch (error) { message(error.message); }
    });
    document.getElementById('work-save-validate').addEventListener('click', async () => {
      try {
        const html = await serialized();
        const result = await post('/api/save', savePayload(html));
        if (result) { baselineDocumentSnapshot = documentSnapshot(); message(result.noOp ? `\u691c\u8a3c\u6e08\u307f\u30fb\u5909\u66f4\u306a\u3057\u3002revision ${result.documentRevision || result.revision}` : `\u4fdd\u5b58\u30fb\u691c\u8a3c\u3057\u307e\u3057\u305f\u3002revision ${result.documentRevision || result.revision}`); }
      } catch (error) { message(error.message); }
    });
    document.getElementById('work-revert').addEventListener('click', async () => {
      try {
        const result = await post('/api/revert', revertPayload());
        if (result) await loadLatestDocument();
      } catch (error) { message(error.message); }
    });
    document.getElementById('work-reload').addEventListener('click', async () => {
      if (reloadInProgress) return;
      reloadInProgress = true;
      controls.forEach(button => { button.disabled = true; });
      message('\u6700\u65b0\u72b6\u614b\u3092\u753b\u9762\u306b\u53cd\u6620\u4e2d\u3067\u3059\u2026');
      try { await loadLatestDocument(); }
      catch (error) { message(error.message); }
      finally {
        reloadInProgress = false;
        controls.forEach(button => { button.disabled = false; });
      }
    });
    const afterInitialRender = () => new Promise(resolve => {
      requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(resolve, 500)));
    });
    afterInitialRender().then(() => refreshStatus()).then(() => {
      const pollStatus = async () => {
        if (refreshInProgress || reloadInProgress) return;
        refreshInProgress = true;
        try { await refreshStatus({announce:false}); }
        catch (error) { message(error.message); }
        finally { refreshInProgress = false; }
      };
      const statusTimer = setInterval(
        pollStatus, 2000,
      );
      window.addEventListener('pagehide', () => clearInterval(statusTimer), {once:true});
    }).catch(error => message(error.message));
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
                    self._html(inject_work_toolbar(self.server.storage.html_response()))
                elif path == "/api/revision":
                    self._json(HTTPStatus.OK, self.server.storage.revision_status())
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
                        replace_slide_ids = payload.get("replaceSlideIds", [])
                        if not isinstance(replace_slide_ids, list) or not all(isinstance(value, str) and value for value in replace_slide_ids):
                            raise BentoConverterError("replaceSlideIds must be an array of non-empty strings")
                        arguments["replace_slide_ids"] = set(replace_slide_ids)
                        if path == "/api/save":
                            scoped_slide_ids = payload.get("scopedSlideIds")
                            if scoped_slide_ids is not None:
                                if (
                                    not isinstance(scoped_slide_ids, list)
                                    or not scoped_slide_ids
                                    or not all(isinstance(value, str) and value for value in scoped_slide_ids)
                                    or len(set(scoped_slide_ids)) != len(scoped_slide_ids)
                                ):
                                    raise BentoConverterError(
                                        "scopedSlideIds must be a non-empty array of unique non-empty strings"
                                    )
                                arguments["scoped_slide_ids"] = set(scoped_slide_ids)
                            operation = payload.get("operation", "authoring-save")
                            if operation not in {"authoring-save", "browser-edit", "segment-import", "segment-replace"}:
                                raise BentoConverterError("Unsupported authoring save operation")
                            report_details = payload.get("operationReport")
                            if report_details is not None and not isinstance(report_details, dict):
                                raise BentoConverterError("operationReport must be an object")
                            report_path = payload.get("reportPath")
                            if report_path is not None and not isinstance(report_path, str):
                                raise BentoConverterError("reportPath must be a repository-relative string")
                            result = self.server.storage.save_serialized(
                                serialized_html, **arguments, operation=operation,
                                report_details=report_details,
                                report_path=(self.server.storage.repository / report_path) if report_path else None,
                            )
                        else:
                            result = self.server.storage.validate_serialized(serialized_html, **arguments)
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
