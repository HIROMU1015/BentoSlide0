# Work editor finalization

The Work editor serves an existing Bento runtime on `127.0.0.1` and persists final Bento document edits without modifying that runtime.

## Start

```powershell
python -m scripts.run_bento_work_editor `
  --source output/presentation.generated.bento.html `
  --target output/presentation.final.bento.html `
  --registry output/diagnostics/merged-registry.json `
  --port 8765
```

Open `http://127.0.0.1:8765/`. The server refuses non-loopback bind addresses. The first start copies generated to final; later starts retain final. Use `--reset-final` for an intentional replacement. Use `--allow-content-edit` only when changing textual/media content is explicitly authorized.

## API

| Endpoint | Purpose |
|---|---|
| `GET /` | Serve final with a temporary save toolbar |
| `GET /api/status` | Return revision, runtime fingerprint, backup count and validation state |
| `GET /api/document` | Return current final JSON and revision |
| `POST /api/validate` | Validate a `serializedHtml` document without saving |
| `POST /api/save` | Save validated `serializedHtml` when `baseRevision` is current |
| `POST /api/revert` | Restore the most recent backup when `baseRevision` is current |

`POST /api/save` extracts only `#bento-doc`; it never trusts or saves the submitted runtime. The response-only loader waits for Bento initialization, then adds the toolbar dynamically and guards `serialize()` so the temporary UI is absent from its result. HTTP 409 rejects a stale SHA-256 revision. Validation errors use HTTP 422 and include contextual `slideId`, `elementId`, and `field` issue strings.

## Storage guarantees

Before replacement, the server validates Bento schema/references, registry and protected content, recursive resource scan, and current runtime fingerprint. It writes HTML and JSON temporary files in the target directory, flushes them, replaces the pair, verifies their equality, and rolls back both on failure. The pre-save pair is copied to `revisions/presentation.final.rev-NNNNNN.bento.{html,json}`; the default retention limit is ten. The toolbar exists only in the HTTP response and never in final HTML.
