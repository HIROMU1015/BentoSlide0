# Primary and supplementary sources

Place papers and non-public supporting material in `sources/private/`. The directory is retained in Git, but its contents are ignored so copyrighted or confidential files are not pushed accidentally.

- With exactly one PDF under `sources/`, the workflow selects it automatically.
- With multiple PDFs, set repository-relative `project.primarySource` in `deck.yaml`; other files remain supplementary candidates.
- If `primarySource` is already set, it must exist, be a PDF, and remain under `sources/`.
- Japanese filenames are supported.

Agents read these as local files only and must not stage or commit private sources. Public, redistributable fixtures may be placed outside `sources/private/` when intentionally tracked.
