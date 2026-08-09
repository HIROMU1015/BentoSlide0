# Primary and supplementary sources

Place papers and non-public supporting material in `sources/private/`. The directory is retained in Git, but its contents are ignored so copyrighted or confidential files are not pushed accidentally.

- `sources/source-manifest.yaml` is the machine-readable source inventory. `authorityMode` is `single`, `multiple`, or `imported`; each item uses a stable ID, repository-relative path, media type, and role.
- With exactly one PDF under `sources/`, the workflow can select it and update state/manifest automatically.
- With multiple PDFs, `project.primarySource` resolves which manifest item is primary; other files remain supplementary candidates.
- If `primarySource` is already set, it must exist, be a PDF, and remain under `sources/`.
- Japanese filenames are supported.

Agents read these as local files only and must not stage or commit private sources. Registry v2 provenance points to stable manifest source IDs. Public, redistributable fixtures may be placed outside `sources/private/` when intentionally tracked. Imported HTML originals remain isolated under `imports/` and are handled by the static importer rather than treated as trusted pages.

User-supplied and agent-created image files live in the separate visible `images/` library. `images/user/` is the intake folder, `images/extracted/` keeps PDF/source crops, and `images/generated/` keeps generated explanatory art. Those library files remain local and become build inputs only after the transactional visual registration flow copies them into the hidden `deck/assets/` registry source of truth.
