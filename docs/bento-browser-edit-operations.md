# Reusable Bento browser edit operations

Small Bento changes use one revision-checked edit plan instead of a new one-off script. In authoring mode, the executor applies supported changes deterministically and saves only requested slides through the running Work editor API. It does not depend on visible control events, so rich-text color changes cannot silently become a no-op. Finalization continues to operate the visible Bento controls. Both routes compare slide hashes, save once, and capture only changed slides. They never write Bento HTML, JSON, or registry files directly.

## Quick semantic operations

Routine authoring edits should use `scripts.apply_bento_quick_edits` before hand-writing element operations. It accepts a 1-based slide number or exact slide ID and classifies the slide into `background`, `header`, `title`, `body`, and `footer`. Combined groups such as `body-text` and `body-shapes` are available, and a target can be narrowed with `elementIds`, `idContains`, `types`, or `excludeIds`.

The quick compiler supports group movement, text/shape styling, group resizing, alignment, equal distribution, and fitting a group within slide margins. It resolves one current `/api/document` snapshot, binds the generated primitive plan to that revision, rejects out-of-slide geometry before saving, then uses the same scoped Work editor API and targeted evidence path documented below. A revision change during compilation fails closed instead of replaying a stale edit.

For the common chat instruction “slide 4 body down 10 px”, no element inventory or custom JSON is required:

```powershell
python -m scripts.apply_bento_quick_edits --editor-url http://127.0.0.1:8877/ `
  --slide 4 --target body --move-y 10
```

The same direct route accepts `--text-color`, `--shape-fill`, `--shape-stroke`, `--align`, `--scale-x`, `--scale-y`, and `--fit-margin`. Use a `bento/quick-edit-plan/v1` JSON plan only when several semantic operations or narrowed targets need to be saved together. `--dry-run` returns resolved element IDs, before/after group bounds, and the generated primitive plan without saving.

The command-line executor automatically selects `authoring-api` for authoring and `browser-ui` for finalization. A screenshot browser is opened only when screenshots are requested. Do not retry a failed visible authoring control before using the API route; that retry is no longer part of the normal path.

## Supported operations

| Operation | Purpose | Authoring | Final |
|---|---|---:|---:|
| `set-shape` | Fill, stroke, geometry, opacity, shadow | yes | yes |
| `set-text-style` | Text color, typography, geometry, opacity | yes | yes |
| `set-geometry` | Geometry for non-text/non-shape native elements | yes | no |
| `set-text` | Replace ordinary editable text | yes | no |
| `set-order` | Front/back and one-layer movement | yes | yes |
| `add-rectangle` | Add a rectangle or a correctly layered background card | yes | no |
| `duplicate` | Duplicate the selected element | yes | no |
| `delete` | Delete the selected element | yes | no |
| `align` | Align two or more selected elements | yes | yes |

`set-text` refuses registered equations. Source-backed elements and registry-bearing content remain subject to the existing Work editor validation and provenance rules.

## Plan example

```json
{
  "format": "bento/browser-edit-plan/v1",
  "operations": [
    {
      "op": "add-rectangle",
      "slideId": "comparison-slide",
      "order": "background-card",
      "properties": {
        "fill": "#EAF2FB",
        "stroke": "#4E79A7",
        "lineWidth": 2,
        "radius": 14,
        "x": 64,
        "y": 270,
        "w": 565,
        "h": 137
      }
    }
  ]
}
```

`background-card` places the new rectangle behind editable content but above the full-slide background. This avoids the hidden-card failure caused by an unconditional send-to-back operation.

## Validation scope

Each edit report names requested and actually changed slide IDs. In finalization, any pre-save change outside the requested slides blocks the save. In authoring mode, the save API merges only `scopedSlideIds` into the current canonical document, so pre-save changes outside the scope are recorded in `preSaveUnrequestedSlideIds` and filtered by the server. Runtime serialization drift on other slides or document-level fields is discarded, preserved in `discardedSlideIds` / `discardedDocumentFields` as evidence, and never written to HTML/JSON. Slide addition, removal, or reordering is rejected. The edit run captures only saved slides and records `fullDeckBrowserCheck: deferred`.

Use the targeted browser check during iteration:

```powershell
python -m scripts.check_bento_browser presentation.authoring.bento.html `
  --slide-id comparison-slide `
  --screenshots-dir output/changed-slides
```

The browser-check CLI reuses `.bento-cache/browser-check` by default. Repeat `--slide-id` for related slides, use `--refresh-cache` to replace cached evidence, or `--no-cache` for the final cache-disabled whole-deck check. A targeted check is never reported as whole-deck approval evidence.

## Save and post-save evidence

The runtime release check and a saved document's `https://sync.bento.page/d/...` collaboration-room probe remain network-blocked. Their narrowly matched read-only probes are recorded as expected blocked requests; no remote connection is allowed.

The executor checks for unknown blocked requests before saving. If the document is committed but a later reload, screenshot, or browser-evidence step fails, the report uses `saveStatus: committed`, `validation: failed`, and `validationScope: post-save`. The CLI exits with code `3` and writes the report instead of incorrectly claiming that the edit itself was not saved. Fix or repeat only the post-save evidence step; do not apply the same edit again.

Authoring edits verify the committed artifact with a lightweight snapshot request and do not reload and initialize the whole editor again. Finalization retains the full reload check because its protected-final persistence contract differs.

If every requested value already matches the current document, the executor returns `saveStatus: no-op` without pressing save, creating a backup, advancing the revision, reloading, or taking screenshots.
