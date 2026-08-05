# BentoSlide0

Deterministic conversion tools for producing editable Bento Slides HTML. The default pipeline is HTML-first: GPT authors fixed-size HTML/CSS plus a registry JSON, Chromium resolves the computed layout, and the converter emits native Bento elements into an unchanged official Bento runtime.

The previous coordinate-design JSON pipeline remains available as a legacy compatibility path.

## Local-first paper-to-Bento workflow

This repository can be copied or cloned as a self-contained production workspace. Normal operation does not require manually selecting ports or running individual Python modules:

1. Clone or copy the repository for one deck.
2. Put the source paper in `sources/private/` (or record another repository-relative path in `deck.yaml`).
3. Optionally refine the request in `REQUEST.md`.
4. In ChatGPT Work, say: `この資料を作成して`.
5. Review the proposed content, then say: `この方針で進めて`.
6. Review each chapter in the local HTML preview. Say `次へ`, or give only the visual correction, after each review.
7. Ask Codex: `BentoSlideに変換して`.
8. In ChatGPT Work, say: `最終調整を開始して`, then make final layout edits and save through the Work editor toolbar.

`deck.yaml` is the machine-readable workflow state. When a local service is needed, double-click `start_deck_workspace.cmd`; it reads the stage and starts only the appropriate service: HTML preview on port 4173 while authoring/reviewing, or the existing Bento Work editor on port 8765 during finalization. Open the copied URL in the ChatGPT Work browser. `stop_deck_workspace.cmd` safely stops the recorded service. Neither launcher opens a normal browser, resets an existing final, nor enables content editing.

Configured repository-relative generated/final paths are honored by the stage-aware launcher. A blocked workflow retains its previous stage and resumes through validated `deck_workflow resume`; final handoff also records an immutable content/structure baseline so final layout and style can change without allowing external content replacement.

Start with [START_HERE.md](START_HERE.md). The full state model and short-command routing are documented in [workflow/WORKFLOW.md](workflow/WORKFLOW.md); `AGENTS.md` provides the compact agent entry point.

### Developer setup

```powershell
python -m pip install -r requirements-browser.txt
python -m playwright install chromium
python -m scripts.deck_workflow validate
python -m scripts.deck_workflow status
```

## HTML-first build

Requirements:

```powershell
python -m pip install -r requirements-browser.txt
python -m playwright install chromium
```

Build one or more lexically sorted chapters:

```powershell
python -m scripts.build_bento_from_html `
  --html-dir chapters/ `
  --registry-dir chapters/ `
  --base Bento_Slides.base.bento.html `
  --output output/presentation.generated.bento.html
```

The build and finalization workflow creates:

```text
output/
├── presentation.generated.bento.html
├── presentation.generated.bento.json
├── presentation.final.bento.html
├── presentation.final.bento.json
├── conversion-report.json
├── screenshots/
│   ├── source/
│   └── bento/
├── revisions/
└── diagnostics/
    ├── browser-check.json
    ├── computed-layout.json
    ├── merged-registry.json
    └── resource-scan.json
```

`presentation.generated.bento.html` differs from the selected base only inside `script#bento-doc`. Local image, media/poster, chart, SVG/foreignObject, and CSS `url(...)` resources are embedded as data URIs; external SVG fragments are retained. A recursive final-document scan rejects unresolved resources. Critical crop failures, malformed registries, missing protected content, broken references, runtime mutations, and serialize failures also fail the build.

## Work editor finalization

### Direct Windows editor launcher

`start_deck_workspace.cmd` is the normal stage-aware entry point. After the HTML-first build has produced the default files, the lower-level editor launcher remains available:

1. Double-click `start_bento_editor.cmd` in Explorer.
2. Open `http://127.0.0.1:8765/` in the ChatGPT Work browser, or reload the BentoSlide tab already left open.
3. Edit and save locally with the Work editor toolbar.
4. Double-click `stop_bento_editor.cmd` when the background editor should stop.

The black command window does not need to remain open, and the launcher does not open Chrome, Edge, or another normal browser. It copies the URL to the Windows clipboard, never supplies `--reset-final`, and continues an existing final instead of replacing it. Keep the Work browser tab open between sessions and reload it after the next start.

Use `start_bento_editor.cmd -Port 8766` when the default port is occupied. Launcher state and logs are written under `output/` as `work-editor.pid`, `work-editor-session.json`, `work-editor.log`, `work-editor.stdout.log`, and `work-editor.error.log`. See [Work editor finalization](docs/work-editor-finalization.md) for custom source/target/registry paths and safe-stop behavior.

The direct localhost command remains available for non-launcher use:

```powershell
python -m scripts.run_bento_work_editor `
  --source output/presentation.generated.bento.html `
  --target output/presentation.final.bento.html `
  --registry output/diagnostics/merged-registry.json `
  --port 8765
```

Open `http://127.0.0.1:8765/` in the ChatGPT Work browser. On first start, `final` is copied from `generated`; an existing final is never overwritten unless `--reset-final` is supplied. The injected toolbar saves `window.bento.serialize()` through a revision-checked API. Only `#bento-doc` is persisted, the runtime stays byte-identical, `presentation.final.bento.json` is synchronized, and prior revisions are retained under `output/revisions/`. Content edits are rejected by default; use `--allow-content-edit` only when explicitly intended.

See [the authoring contract](docs/html-first-authoring-contract.md), [conversion specification](docs/html-to-bento-conversion-spec.md), [Work editor finalization](docs/work-editor-finalization.md), [source-of-truth policy](docs/source-of-truth-policy.md), [native support matrix](docs/bento-native-feature-support.md), and [fallback policy](docs/fallback-policy.md).

## Legacy JSON-first build

The old deterministic entry point is intentionally preserved:

```powershell
python -m scripts.build_bento `
  --base Bento_Slides.base.bento.html `
  --design gpt_bento_design.json `
  --output demo.generated.bento.html
```

Its contract remains documented in [bento-conversion-spec.md](docs/bento-conversion-spec.md).

## Verification

```powershell
python -m unittest discover -v
$env:BENTO_BROWSER_TEST = "1"
python -m unittest discover -v
python -m scripts.check_html_first_determinism --html-dir tests/fixtures/html_first --registry-dir tests/fixtures/html_first --base Bento_Slides.base.bento.html --report determinism-report.json
Remove-Item Env:BENTO_BROWSER_TEST
```

The suite also validates the workflow state machine, atomic `deck.yaml` updates, source discovery, stage-aware output gates, loopback-only HTML preview, traversal rejection, and Windows launcher identity checks. The browser-gated suite covers the feature matrix and the localhost Work editor, including browser serialize/save/reload, revision conflict rejection, runtime preservation, media poster embedding, external SVG fragments, recursive resource scanning, chapter combination, native rendering, localized fallback, visual comparison, and determinism.

GitHub Actions uploads the complete `html-first-evidence` artifact and writes a job summary with test count, native/fallback and visual results, Work editor save/conflict/runtime checks, poster/fragment/recursive-scan counts, unresolved resources, serialize and determinism status, and HTML/Bento JSON SHA-256 values.
