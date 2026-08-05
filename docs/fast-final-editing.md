# Fast final editing

Use this route after Bento finalization has begun and `output/presentation.final.bento.html` is authoritative. It turns an exact edit request into one validated patch, one save, and one browser reload.

## Choose the route

| Request | Fast route |
|---|---|
| Exact position, size, rotation, opacity, fill, stroke, text styling, or effect | Batch patch |
| Theme, slide background, or element stacking order | Batch patch |
| Several exact presentation changes | Put all changes in one batch patch |
| Visual balancing, dragging, or an editor-only control | Browser UI, then Work editor save |
| Text/equation content, media, chart/table data, IDs/types, slide structure, notes, behavior, or references | Do not use this route; return to the authoritative pre-final source and required approval/reset workflow |

Do not rerun conversion into final. Do not use `--reset-final` or `--allow-content-edit` for an ordinary layout request.

## Apply one batch

Create a JSON patch inside the repository:

```json
{
  "format": "bento/final-presentation-patch/v1",
  "description": "Enlarge and restyle the right-side shape",
  "elementEdits": [
    {
      "slideId": "demo-slide-1",
      "elementId": "slide-1-shape",
      "set": {
        "x": 600,
        "w": 570,
        "fill": "#2563EB"
      }
    }
  ]
}
```

Run a dry check when the coordinates or styling are uncertain, then save once:

```powershell
python -m scripts.apply_bento_final_edits --patch path/to/final-edit.json --dry-run
python -m scripts.apply_bento_final_edits --patch path/to/final-edit.json
```

With default paths, the command reads generated/final paths from `deck.yaml`, requires stage `bento_finalization` or `complete`, requires `diagnostics/merged-registry.json`, and verifies the current and proposed final against `validation.finalBaseline`. For isolated tests only, pass `--source` and `--target` together. Add `--report path/to/report.json` when durable machine-readable evidence is useful.

Reports are installed atomically. A report cannot overwrite the patch, generated/final HTML, either sidecar, registry, immutable baseline, Work editor save report, `deck.yaml`, or anything in the revisions directory. An existing file is replaceable only when it is already a fast-edit result report.

After a successful save, reload the already-open Work browser once and inspect only the affected slides. Do not reload between individual edits in the same request.

## Patch fields

Top-level keys are `format`, optional `baseRevision`, optional `description`, `documentSet`, `slideEdits`, `elementEdits`, and `zOrders`.

- `documentSet`: `theme`
- `slideEdits[].set`: `background`
- `elementEdits[].set`: `x`, `y`, `w`, `h`, `z`, `zIndex`, `rotation`, `opacity`, `shadow`, `blur`, `blend`, `backdropFilter`, `fx`, `fontSize`, `fontFamily`, `fontWeight`, `color`, `colorGradient`, `align`, `valign`, `lineHeight`, `letterSpacing`, `textStroke`, `fill`, `fillGradient`, `stroke`, `strokeWidth`, `radius`, `strokeDash`, `strokeStyle`, `fit`, `style`
- `zOrders[].elementIds`: every existing element ID on that slide, exactly once, in the desired document order

Use `baseRevision` when edits were prepared from a previously inspected document; a stale revision is rejected rather than overwriting newer work. Unknown targets, duplicate edits, unsupported fields, incomplete z-order lists, schema failures, registry failures, immutable-baseline violations, protected-content changes, invalid resources, out-of-canvas frames, and report collisions are rejected before replacement.

## Save guarantees

The command updates only the final `#bento-doc`, preserves the runtime, verifies the registry and immutable protected-content baseline before and after editing, synchronizes the `.bento.json` sidecar, and creates the normal Work editor revision backup. A no-op does not create a backup. The generated source remains unchanged.
