# Static HTML import

General HTML is untrusted input. Place the untouched original below `imports/` and run:

```powershell
python -m scripts.import_html_deck --input imports/source.html --slide-selector ".slide"
```

The importer determines a slide selector only when unambiguous; otherwise it stops and asks for `--slide-selector`. The normalized deck is fixed-size, static `deck/deck.preview.html` plus `deck/deck.registry.json`, with a machine-readable import report and updated `sources/source-manifest.yaml`/`deck.yaml` in one transaction. Use `--force` only to explicitly replace an existing imported authoring source.

Security rules:

- scripts are never executed and do not enter normalized HTML;
- external network requests are blocked and no remote resource is fetched implicitly;
- inline event handlers and `javascript:` URLs are removed/reported;
- `iframe`, `object`, and `embed` are removed/reported;
- remote image, font, stylesheet, and media URLs are reported;
- local assets are resolved under the import boundary; optional `--copy-assets` copies safe dependencies;
- preview and conversion use only normalized output, never the original.

The importer is a deterministic static migration aid, not a browser application runner. Dynamic behavior is deliberately not reproduced.
