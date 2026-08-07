# Work editor authoring and finalization

The Work editor serves an existing Bento runtime on `127.0.0.1`. Its explicit `authoring` policy persists a document and writable registry; its `finalization` policy preserves the frozen registry and permits presentation-only document edits. Neither mode modifies the runtime.

## Start

### Windows one-click operation

For the full local workflow, double-click `start_deck_workspace.cmd`. It reads `deck.yaml`: HTML stages start the local preview, `bento_authoring`/`content_review` start authoring mode with configured generated/authoring/registry paths, and `bento_finalization` starts finalization mode with configured authoring/final/final-registry paths. Use `stop_deck_workspace.cmd` to stop the recorded workspace service safely.

The lower-level editor-only launcher remains available. From Explorer, double-click `start_bento_editor.cmd`. It resolves the repository from its own location, starts the editor as a hidden independent process, copies `http://127.0.0.1:8765/` to the clipboard, and then closes. Keep the ChatGPT Work browser tab open and reload it on later sessions. It never opens a normal browser or controls ChatGPT Work.

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

The start launcher first validates `/api/status`. The same target and port is reported as already running without spawning another process. A different Bento target or any unrelated service on the port is left untouched and returns an error suggesting `start_bento_editor.cmd -Port 8766`. An exclusive `output/work-editor-launcher.lock` file, held with `FileShare.None` only during launcher work, rejects simultaneous starts.

State is stored in `output/work-editor.pid` and `output/work-editor-session.json` using format `bento/work-editor-session/v1`. The session records PID, launcher/start timestamps, repository, absolute source/target/registry paths, loopback host, port, and URL. `output/work-editor.log` records launcher metadata and captured startup output; raw stdout and stderr remain in `output/work-editor.stdout.log` and `output/work-editor.error.log`. One `.previous.log` generation is retained.

The launcher never passes `--reset-final` or `--allow-content-edit` and never runs conversion. It derives every source, target, and registry path from `deck.yaml`; an existing protected artifact is retained.

### Direct authoring command

```powershell
python -m scripts.run_bento_work_editor `
  --mode authoring `
  --source output/presentation.generated.bento.html `
  --target output/presentation.authoring.bento.html `
  --source-registry output/diagnostics/merged-registry.json `
  --target-registry output/presentation.authoring.registry.json `
  --repository . `
  --port 8765
```

### Direct finalization command

```powershell
python -m scripts.run_bento_work_editor `
  --mode finalization `
  --source output/presentation.authoring.bento.html `
  --target output/presentation.final.bento.html `
  --registry output/presentation.final.registry.json `
  --repository . `
  --port 8765
```

Open `http://127.0.0.1:8765/`. The server refuses non-loopback bind addresses. Prefer the workflow transition for initialization: it creates authoring/final artifact sets and state atomically. Compatibility flags remain available only for deliberate exceptional recovery.

## Fast deterministic edits

For exact geometry, style, theme, slide background, or z-order changes, use one validated batch instead of typing values into the browser one field at a time:

```powershell
python -m scripts.apply_bento_final_edits --patch path/to/final-edit.json --dry-run
python -m scripts.apply_bento_final_edits --patch path/to/final-edit.json
```

The command uses the same `WorkEditorStorage` validation, revision check, backup, runtime-integrity check, HTML/JSON synchronization, and protected-content boundary as the localhost editor. It requires an existing final and, with schema v2 default paths, a `deck.yaml` stage of `bento_finalization` or `complete`, the frozen final registry, and verified immutable document/registry baselines. It checks the current and proposed final against those records before saving. It never initializes or resets final from generated/authoring. After saving, reload the existing Work browser once and inspect the affected slide. See `docs/fast-final-editing.md` for the patch format, routing rules, examples, and report-path protections.

## API

| Endpoint | Purpose |
|---|---|
| `GET /` | Serve final with a temporary save toolbar |
| `GET /api/status` | Recover first; return repository, mode, targets, document/registry revisions, runtime, backup and validation state |
| `GET /api/document` | Return one consistent document/registry snapshot and both revisions |
| `POST /api/validate` | Validate a `serializedHtml` document without saving |
| `POST /api/save` | Save when `baseDocumentRevision` and `baseRegistryRevision` are current; authoring may include `registry` |
| `POST /api/revert` | Restore the most recent complete artifact-set backup against both revisions |

`POST /api/save` extracts only `#bento-doc`; it never trusts or saves the submitted runtime. Authoring responses contain `documentRevision`, `registryRevision`, `contentApprovalInvalidated`, and `transactionId`. Registry omission retains the current registry only after its revision and all document references validate. HTTP 409 rejects either stale SHA-256 revision; HTTP 422 reports contextual validation issues.

The response-only loader waits for Bento initialization, adds the toolbar dynamically, and guards `serialize()` so temporary UI is absent. The guard preserves Bento's public contract: `window.bento.serialize()` remains synchronous and returns its HTML string directly. It detaches the toolbar immediately before serialization and restores the exact DOM parent/position in `finally`, including when serialization throws. Save/validate/revert/reload remain usable afterward.

## Storage guarantees

Before serving requests, the server recovers unfinished journals and acquires the OS-level writer lease for its repository/artifact identity. Authoring commits HTML, JSON, registry, approval-invalidating `deck.yaml` state, backups, and evidence through the common journal transaction. Finalization commits the protected HTML/JSON pair with the frozen registry as validation input. `GET /`, status, and document responses use storage-level consistent snapshots and never see partial replacement. A second writer for the same set is refused; an unrelated deck does not conflict.

The default revision retention limit is ten. Toolbar/loader/style/host identifiers exist only in the HTTP response and never in persisted HTML. Operation-report failure after validation retains committed artifacts in `report_failed` and is repaired by the next recovery rather than rolling them back.

The repository workflow additionally records a finalization baseline in the target revisions directory. Completion accepts geometry, presentation styling, theme/background, and z-order differences from that baseline, while rejecting content, slide/element identity or structure, equations, chart/table/media data, notes, behavior, and references. This catches a final file replaced outside the Work editor without making mutable generated output authoritative after finalization begins.
