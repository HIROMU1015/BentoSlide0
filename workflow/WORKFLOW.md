# Local BentoSlide workflow

`deck.yaml` is the sole machine-readable workflow state. Agents use `python -m scripts.deck_workflow`; users are not expected to run state commands or name files.

## Stages

| Stage | Owner | Source of truth | Exit condition |
| --- | --- | --- | --- |
| `initialized` | Work | `sources/` | primary paper resolved |
| `planning` | Work | `planning/` | explanation, story, slide plan, and chapter list written |
| `awaiting_plan_approval` | Work | `planning/` | user approves the material plan |
| `html_authoring` | Work | chapter HTML + registry | current pair is structurally complete |
| `html_review` | Work | chapter HTML + registry | user approves the major visual composition |
| `ready_for_conversion` | Codex | approved chapters | every chapter is complete and paired |
| `converting` | Codex | approved chapters | current HTML-first build and diagnostics exist |
| `bento_validation` | Codex | generated Bento | generated/final pairs and diagnostics pass |
| `bento_finalization` | Work | final Bento `#bento-doc` | user approves final layout and technical checks pass |
| `complete` | Codex | final Bento `#bento-doc` | final verification recorded |
| `blocked` | Work or Codex | last valid source | blocking reason reported without unsafe transition |

Expected owners and sources of truth are schema/invariant checked. Approval stages are never crossed automatically.

## Agent-facing state commands

```text
status [--json]              inspect state
validate                     schema and invariant validation
discover-sources [--json]    resolve PDF candidates
initialize                   initialized -> planning
configure-chapters ...       register the complete planned chapter list
submit-plan                  planning -> awaiting_plan_approval
approve-plan                 awaiting_plan_approval -> html_authoring
begin-chapter                select an incomplete chapter
complete-chapter             validate pair -> html_review
approve-chapter              record visual approval; select next or become ready
prepare-conversion           ready_for_conversion -> converting
mark-converted               verify generated diagnostics and safely initialize/retain final
begin-finalization           bento_validation -> bento_finalization
approve-final                record human approval after final technical checks
complete                     bento_finalization -> complete
block                        record an explicit blocker and owner
```

Writes to `deck.yaml` use a same-directory temporary file, flush/fsync, and atomic replacement. Transitions validate real files, not only the stage string.
The four generated/final HTML/JSON paths must be distinct. A blocked transition records its non-empty reason in `workflow.blockingReason`; outside `blocked` that field is null.

## Short-command routing

### この資料を作成して

Read `REQUEST.md`, discover PDFs, resolve the primary source, create the three planning documents, register every planned chapter, submit the plan, and ask only for content-level approval. If zero or multiple PDFs prevent safe selection, ask only for the missing material decision.

### この方針で進めて

Record plan approval, begin the first incomplete chapter, create/update its paired fixed-size HTML and registry, start or refresh `http://127.0.0.1:4173/`, validate the pair, and request major visual approval.

### 次へ

While in `html_review`, record the current visual approval and automatically select the next incomplete chapter. When every registered chapter is approved, move to `ready_for_conversion`. Never ask for a chapter number.

### BentoSlideに変換して

Require `ready_for_conversion`, validate every pair and approval, transition to `converting`, run the existing `scripts.build_bento_from_html` command into `outputs.generatedHtml`, inspect all diagnostics/browser evidence, call `mark-converted`, and then `begin-finalization`. Final initialization uses existing `WorkEditorStorage` behavior with `reset_final=False`; an existing final is retained.

### 最終調整を開始して

Require `bento_finalization`, start the existing Windows Bento editor launcher, and direct the user to the localhost URL. Treat final `#bento-doc` as authoritative. Default edits are geometry/presentation style only; save through the Work editor, reload, validate, obtain final approval, then complete technical verification.

## Handoffs

- Work -> Codex: all plan approvals and all chapter visual approvals are recorded; `handoff.readyForCodex` is true and `prepare-conversion` succeeds.
- Codex -> Work: generated diagnostics and browser checks pass; final HTML/JSON is initialized or retained without reset; `begin-finalization` sets `handoff.readyForFinalEditing`.

Never trigger conversion from a workspace launcher. Never rebuild into the final path. Structural regeneration after final editing requires explicit user authorization and a deliberate final reset outside normal operation.
