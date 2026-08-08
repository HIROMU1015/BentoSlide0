# Local BentoSlide workflow

`deck.yaml` is the sole machine-readable state. Agents use `python -m scripts.deck_workflow`; users do not run state commands, choose IDs, or name files.

## Stages

| Stage | Owner | Source of truth | Exit condition |
| --- | --- | --- | --- |
| `initialized` | Work | sources | primary source resolved |
| `planning` | Work | sources + planning | policy, story, slide plan, section list exist |
| `awaiting_plan_approval` | Work | planning | user approves material plan |
| `html_authoring` | Work | single HTML/registry or migrated chapters | current section/chapter complete |
| `html_review` | Work | same HTML/registry | current visual composition approved at current digest |
| `ready_for_conversion` | Codex | all approved HTML units | every approval digest remains current |
| `converting` | Codex | approved HTML units | deterministic build/evidence exists |
| `bento_validation` | Codex | generated Bento + generated registry | generated bundle passes and authoring artifacts initialize/retain safely |
| `bento_authoring` | Work | authoring Bento HTML/JSON/registry | content/structure edits validate |
| `content_review` | Work | authoring Bento HTML/JSON/registry | exact document and registry revisions approved |
| `bento_finalization` | Work | final `#bento-doc`, frozen final registry, baselines | presentation-only edits and final approval pass |
| `complete` | Codex | final artifacts and baselines | final technical verification recorded |
| `blocked` | Work or Codex | saved pre-block source | reason resolved and `resume` revalidates it |

Expected owner/source and real artifacts are invariant checked. Approval stages are never crossed automatically.

## Rolling section lifecycle (primary UX)

New schema v2 single/imported decks are normally completed section by section:

```text
planned -> html_authoring -> html_review -> bento_integration -> bento_authoring -> accepted
```

`canonical` is exactly one of `planning`, `html`, or `bento`. HTML approval records the current section digest and authorizes `promote-current-section`; it does not accept the Bento result. Promotion converts only that section and inserts or replaces its slides in planning order. The promoted HTML becomes a historical snapshot; later Work editor changes stay Bento-canonical and are not synchronized back. `finish-current-section` accepts the current Bento revision and opens the next section. Accepted sections can be reopened through Bento or, for a deliberate redesign, through a fresh HTML candidate. After all sections are accepted, whole-deck `content_review` is mandatory.

A content/structure request made in finalization or after completion reopens the affected authoring section. It invalidates whole-deck/final approval and requires section acceptance plus whole-deck approval again. It never enables content edits in final mode and never silently overwrites an existing final artifact set.

Natural conversation is routed internally to the high-level operations below. `advance` performs safe mechanical work only and stops at human approval checkpoints.

```text
advance / approve-current / promote-current-section / edit-current
finish-current-section / reopen-current-section / review-whole-deck
capture-request / route / status [--json]
```

## State commands

```text
status [--json]                 consistent status; refresh stale content approval
route [--json]                  deterministic primary-workspace route
capture-request --text ...      persist the conversational brief in REQUEST.md
advance                         move to the next human checkpoint, never approve
approve-current                 approve only the displayed plan/HTML/content checkpoint
promote-current-section         section-only conversion and authoring transaction
promote-section --section ...   explicit-ID compatibility form of the same transaction
edit-current                    resolve the current editable workspace
finish-current-section          accept the current Bento section revision
reopen-current-section          resume an accepted section via Bento or HTML
review-whole-deck               mandatory review after every section is accepted
validate                        schema, path, stage, source, and artifact invariants
migrate [--dry-run]             idempotent schema v1 -> v2 migration
set-project --kind ... --title  schema v2 early-stage project metadata only
discover-sources [--json]       resolve manifest/PDF candidates
initialize                      initialized -> planning
configure-sections ...          register single-file planned sections
configure-chapters ...          legacy migrated modular equivalent
submit-plan                     planning -> awaiting_plan_approval
approve-plan                    -> html_authoring
begin-section                   choose the first incomplete section
complete-section                validate source -> html_review
approve-section                 store digest; choose next or become ready
unlock-section                  invalidate one approved section deliberately
prepare-conversion              ready_for_conversion -> converting
mark-converted                  validate generated; initialize/retain authoring
begin-authoring                 bento_validation -> bento_authoring
begin-content-review            validate authoring -> content_review
approve-content                 bind approval to both current revisions
reset-authoring-from-html       explicit full reset; authoring stage only
begin-finalization              approved content -> final artifacts/baselines
approve-final                   final technical check + human approval
complete                        bento_finalization -> complete
reopen-finalization             invalidate final approval and resume presentation edits
block / resume                  preserve and revalidate the complete prior tuple
```

State writes are atomic. Artifact-changing state transitions use the durable multi-artifact transaction layer where required. All repository-relative paths are traversal checked, generated/authoring/final paths are distinct, and sidecar paths must match their HTML names.

`set-project` is an agent-facing setup command, not an additional user short phrase. It is limited to schema v2 `initialized`/`planning`, changes only `project.kind` and `project.title`, and leaves stage and approvals unchanged. Blocked workflows must use `resume` first. The kind must match `^[a-z][a-z0-9_-]*$`; the title must be a non-empty single line.

## Standard single-HTML route

`authoring.mode: single` uses `deck/deck.preview.html` and `deck/deck.registry.json`. Each planned section is a stable grouping of slide IDs. Its approval digest includes canonical section DOM, referenced registry projection, referenced asset bytes, and global CSS/theme. A changed section/registry/asset invalidates that section; changed global CSS/theme invalidates every section. Conversion rechecks all digests.

`authoring.mode: modular` is supported for migrated v1 chapter projects. It retains the chapter approval commands and files; migration alone never changes a later stage into Bento authoring.

## Bento authoring and content approval

`mark-converted` validates generated output and initializes or retains the three authoring artifacts. `begin-authoring` hands them to Work. Authoring mode may change content and structure, with registry changes in the same save. It treats existing ID/type changes as explicit replace operations.

Every save checks both base revisions and validates HTML/JSON/registry, cross references, protected metadata, resources, and runtime before a three-artifact commit. A registry body can be omitted only when the supplied current registry revision is still current and the proposed document validates against that registry. Registry-requiring document changes without the corresponding definitions are rejected.

Authoring may temporarily save provenance drafts. Content review rejects equations without `equationId`, charts without `chartId`, tables without `tableId`, source-backed image/SVG elements without `figureId` or `assetId`, and any element marked `unprovenancedDraft`. Referenced IDs must resolve in the current registry.

Content approval stores current document revision, registry revision, time, and:

```text
sha256(UTF-8("bento/content-approval/v1\0" + documentRevision + "\0" + registryRevision))
```

Current revisions are recomputed on save, status, review, approval, final handoff, segment operations, offline transactions, and migrated-state validation. A mismatch makes the approval pending. Finalization refuses a stale approval.

`begin-finalization` creates final HTML, JSON, final registry, baseline document, baseline registry, and updated state in one transaction. Existing mismatching final artifacts are not overwritten. Final mode freezes content, structure, IDs/types, data, references, and registry; only geometry, presentation style, theme/background, and z-order may change.

Stop the final Work editor before `approve-final`; its lifetime writer lease deliberately prevents approval from racing a save. Final approval binds the document revision, final HTML byte revision, final registry revision, and runtime fingerprint. `complete` recomputes all four and refuses stale approval. Editing a completed or already-approved deck requires `reopen-finalization`, which validates the current bundle, returns to `bento_finalization`, and clears the old approval before any write.

## Short-command routing

### この資料を作成して

Read `REQUEST.md`, resolve the manifest/primary source, create the planning files, register all sections, submit the plan, and ask only for material approval. With zero or ambiguous primary sources, ask only for the missing material decision.

### この方針で進めて

Record plan approval, select the first incomplete section, author/update the single HTML/registry source, start or refresh `http://127.0.0.1:4173/`, validate it, and request visual approval.

### 次へ

In `html_review`, record the current digest and select the next incomplete section. When all sections are approved and current, move to `ready_for_conversion`. Never ask for the section ID.

### BentoSlideに変換して

Require `ready_for_conversion`; validate every approval digest; run the single-file HTML-first build to configured generated paths without `--incremental`; inspect conversion, browser, browser-environment, screenshot, resource, runtime, and determinism evidence; call `mark-converted`, then `begin-authoring`; stop at `bento_authoring`. Interactive preview builds may use the slide cache, but conversion and approval gates always require full build/full validation. Do not start the editor until `BentoSlideで編集を開始して`, and do not initialize final yet.

### BentoSlideで編集を開始して

Require `bento_authoring`, start the stage-aware editor in authoring mode, and expose the same revision-checked authoring HTML/JSON/registry to the user and Work. Do not enter content review or finalization automatically.

### 内容を確定して

Validate the current authoring document/registry and call `begin-content-review`. Request the user's content/structure approval. Do not call `approve-content` from this phrase alone.

### この内容で最終調整へ

Require `content_review`; treat this phrase as explicit approval of the currently displayed content/structure, call `approve-content`, recheck both revisions, and call `begin-finalization`. Any intervening write invalidates approval. This is the only normal authoring-to-final handoff.

### 最終調整を開始して

Require `bento_finalization`, start finalization mode, and adjust layout/style only. Save, reload, validate against both baselines, obtain final approval, and complete.

## Segment and import routes

During `bento_authoring`, segment operations support append/import, insert before/after an anchor, single-slide replacement, contiguous range replacement, and section replacement. Targets remain explicit internally but are inferred from planning/current state for conversational use. Every operation protects outside slide hashes and cross-slide/registry references; generated/final remain unchanged. A running matching editor becomes the sole writer via localhost API. Otherwise the CLI must acquire the same OS lease.

`scripts.import_html_deck` accepts only an original under `imports/`, never executes its scripts, blocks network, sanitizes active content, produces normalized static single HTML/registry, and updates source manifest/state transactionally. Ambiguous slide selection requires `--slide-selector`.

## Recovery, blocking, and migration

Before normal read/write service, unfinished journals are recovered. Full new revisions finish commit; partial replacement rolls the entire set back; all-old targets finish rollback; missing recovery evidence stops without modifying artifacts. Report-only failures keep the committed artifacts and retry the report later.

`block` stores the full prior workflow tuple. `resume` validates that tuple's required files before restoration. Users never edit YAML for recovery.

Schema v1 migration is idempotent and stage-preserving. For `bento_finalization`/`complete`, it verifies the existing final pair, baseline, and merged registry, transactionally snapshots final/baseline registries, and leaves final/revision data untouched. Such late migrations may have null authoring paths only under `migration.lateStageCompatibility`; new v2 decks may not.

Never trigger conversion from a launcher, rebuild into final, or reset protected artifacts without explicit authorization and the dedicated confirmed command.
