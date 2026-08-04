# Work editor finalization

The Work editor serves an existing Bento runtime on `127.0.0.1` and persists final Bento document edits without modifying that runtime.

## Start

### Windows one-click operation

From Explorer, double-click `start_bento_editor.cmd`. It resolves the repository from its own location, starts the editor as a hidden independent process, copies `http://127.0.0.1:8765/` to the clipboard, and then closes. Keep the ChatGPT Work browser tab open and reload it on later sessions. It never opens a normal browser or controls ChatGPT Work.

Stop only when needed by double-clicking `stop_bento_editor.cmd`. The stop launcher verifies the recorded PID, process start time, command line, target, repository, and `/api/status` before using `Stop-Process`. A missing process is treated as an already-stopped stale session; an identity mismatch is an error and the unrelated process is not stopped.

Defaults are `output/presentation.generated.bento.html`, `output/presentation.final.bento.html`, `output/diagnostics/merged-registry.json`, `127.0.0.1`, and port `8765`. PowerShell use and overrides remain available:

```powershell
.\scripts\start_bento_editor.ps1
.\scripts\start_bento_editor.ps1 -Port 8766
.\scripts\start_bento_editor.ps1 `
  -Source output\other.generated.bento.html `
  -Target output\other.final.bento.html `
  -Registry output\diagnostics\other-registry.json
.\scripts\stop_bento_editor.ps1
```

Relative paths resolve from the repository, not the caller's current directory. Python detection checks `.venv`, `venv`, and `env` under the repository, then `py.exe -3`, then `python.exe`; every candidate must successfully import `bento_converter`. `-NoClipboard` disables clipboard access for automation.

The start launcher first validates `/api/status`. The same target and port is reported as already running without spawning another process. A different Bento target or any unrelated service on the port is left untouched and returns an error suggesting `start_bento_editor.cmd -Port 8766`. A repository-derived Windows named mutex rejects simultaneous launcher starts.

State is stored in `output/work-editor.pid` and `output/work-editor-session.json` using format `bento/work-editor-session/v1`. The session records PID, launcher/start timestamps, repository, absolute source/target/registry paths, loopback host, port, and URL. `output/work-editor.log` records launcher metadata and captured startup output; raw stdout and stderr remain in `output/work-editor.stdout.log` and `output/work-editor.error.log`. One `.previous.log` generation is retained.

The launcher never passes `--reset-final` or `--allow-content-edit`, never runs conversion, and never regenerates the registry. Therefore an existing final remains authoritative, while a missing final is created by the unchanged Work editor behavior.

### Direct command

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

`POST /api/save` extracts only `#bento-doc`; it never trusts or saves the submitted runtime. The response-only loader waits for Bento initialization, then adds the toolbar dynamically and guards `serialize()` so the temporary UI is absent from its result. The guard preserves Bento's public API contract: `window.bento.serialize()` remains synchronous and returns its HTML string directly. It removes the toolbar immediately before serialization and restores it in `finally`, including when serialization throws. The Work editor never changes the runtime API's return type, and only a validated `#bento-doc` is persisted. HTTP 409 rejects a stale SHA-256 revision. Validation errors use HTTP 422 and include contextual `slideId`, `elementId`, and `field` issue strings.

## Storage guarantees

Before replacement, the server validates Bento schema/references, registry and protected content, recursive resource scan, and current runtime fingerprint. It writes HTML and JSON temporary files in the target directory, flushes them, replaces the pair, verifies their equality, and rolls back both on failure. The pre-save pair is copied to `revisions/presentation.final.rev-NNNNNN.bento.{html,json}`; the default retention limit is ten. The toolbar exists only in the HTTP response and never in final HTML.
