# Whole-deck HTML change review

The standard schema v2 workflow authors the complete fixed-size HTML/registry deck before conversion. Once that deck reaches HTML review, conversational corrections use review-before-apply rather than direct canonical edits.

## User experience

1. The user describes a concern in ordinary language.
2. Work/GPT identifies the requested slides and assesses the complete deck before authoring a candidate. It checks prerequisites, adjacent transitions, repetition, terminology, numbering/cross-references, conclusions, order, slide count, and section boundaries, then classifies the recommendation as local, related, or structural/global.
   - For a local change, it states that no wider change is recommended.
   - For a related change, it includes and justifies the additional slides.
   - For a materially broader reorder, addition/removal, section split/merge, or story rewrite, it proposes the direction and affected slides before creating that broader candidate.
   Slide-by-slide user messages are accumulated editorial intent, not an instruction to freeze every unmentioned slide. A new message received while a proposal is active requires the pending candidate to be reassessed and, when necessary, cancelled and recreated.
3. Work/GPT creates a temporary complete-deck candidate for the agreed scope.
4. It compares current and candidate HTML/registry and explains:
   - what will change;
   - which slides changed directly;
   - which other slides should be reviewed because of narrative, structure, registry, or shared-style coupling;
   - which slides and global properties remain unchanged.
5. The preview offers both the current deck and candidate without replacing the current deck.
   Its section/slide navigation, slide numbers, titles, and affected-slide order are derived independently from the exact HTML version being shown. Added and removed slides are labelled as existing only in the candidate or current version, and selecting such a label switches to the version where that slide exists.
   The sidebar divider can be dragged, adjusted with the arrow keys, or double-clicked to reset. The preview automatically fits the fixed-size deck to the remaining browser area by applying a runtime-only display scale inside the preview frame; it does not rewrite the canonical/candidate HTML or change the deck's intrinsic slide dimensions.
6. Each affected slide starts as `未確認`. The user can mark the selected slide as `確認済み` or `要修正`. These marks are a browser-local review checklist, not independent slide approvals and not partial application: the candidate remains one complete-deck revision. `要修正` keeps the final action disabled and directs the user back to the chat so Work/GPT can reassess the accumulated request and recreate the candidate when needed.
7. Once every affected slide is `確認済み`, the preview enables `この変更案全体を反映`. Pressing it and accepting the confirmation dialog is explicit confirmation of the exact revision-bound proposal. The server performs approval, transactional application, and the required browser check in sequence. It never applies slides independently.
8. After application, comparison controls and the old candidate view close. The preview shows only the updated canonical HTML and the browser-check result. If the check was interrupted or failed, `自動検証を再実行` resumes that gate without applying the candidate again.
9. After a successful check, `このHTML全体でBentoSlideへ進む` performs the separate whole-deck HTML approval. Conversion remains blocked until that approval is current.

The explanation uses slide titles. IDs, revisions, paths, and CLI commands remain internal unless diagnostics are requested.

## Impact rules

Each new `bento/html-change-proposal/v3` records requested, related, changed, affected, added, and removed slides; slide order and section-membership changes; registry and global-style changes; affected sections; and readable summaries. Read-only compatibility remains for older v2 reports, but an active v2 proposal must be cancelled and recreated before approval because it has no dependency-bound evidence.

- A slide-local DOM change reviews the requested and actually changed slides.
- An explicitly related narrative change reviews both the requested and related slides.
- A registry change reviews every slide in sections whose provenance/dependency digest changed.
- A slide addition, removal, reorder, section move, or shared CSS/theme change reviews the complete deck.

The computed affected list is conservative. The agent may add related slides based on narrative reasoning, but may not omit machine-detected changes.

## Revision and persistence contract

Opening whole-deck review stores an `authoring.htmlReview` baseline for the exact canonical HTML, registry, complete deck evidence, and every referenced local dependency. The dependency manifest includes slide assets, linked stylesheets, recursively imported stylesheets, and CSS resources. Direct canonical edits or dependency drift invalidate the checkpoint; approval never recomputes a new baseline from unreviewed bytes.

The proposal snapshots candidate HTML and registry next to the canonical HTML so local relative assets resolve in preview. A diagnostics report is stored under `output/html-change-proposals/`; neither snapshot nor report is a deliverable or part of generated Bento.

Confirmation is valid only while the four primary byte revisions and both complete dependency manifests remain unchanged:

- canonical HTML;
- canonical registry;
- candidate HTML;
- candidate registry.

The proposal digest binds the four revisions, base/candidate review digests and dependency manifests, candidate paths, user request, readable summary, impact summary, and every machine-computed impact field. Approval records that exact digest and recomputes the complete impact evidence. Drift or tampering in the canonical deck, candidate, any local dependency, explanation, or impact requires a fresh proposal.

The preview mutation endpoint is loopback-only and accepts only same-origin JSON requests carrying the server's per-process action token. Every request repeats the proposal ID, proposal digest, and all four byte revisions, requires an explicit confirmation flag, and is serialized under an action lock. A stale or mismatched request fails without mutation. Previewed deck HTML runs in a sandboxed iframe with scripts disabled, so current or candidate content cannot invoke the parent approval API. The token and checklist are convenience/UI state; `deck.yaml`, proposal evidence, workflow validation, and artifact transactions remain the authority.

Application acquires one union writer lease over canonical HTML/registry, candidate HTML/registry, every base/candidate dependency, state, and report. It re-reads and validates the revisions, dependency manifests, review evidence, and approved proposal digest while that lease is held, then uses the repository artifact transaction layer to replace canonical HTML and registry together with state/report updates. Cancellation never applies candidate bytes and remains available even when a proposal has gone stale. A change to an external stylesheet should therefore be represented by a candidate referencing reviewed stylesheet bytes, never by mutating the current shared stylesheet in place.

Application creates a pending post-apply review bound to the installed HTML/registry revisions, dependency-bound review baseline, and proposal digest. `check-html-change` renders the installed deck with the deterministic browser harness, rejects blocked network activity, checks the visible transformed bounding frame (including rotation), and checks overflow for every text-bearing element regardless of its declared Bento type. It transactionally stores the browser report, environment fingerprint, and one screenshot for every affected slide still present. Removed slides remain listed in proposal impact but cannot have a post-apply screenshot. Any later HTML, registry, dependency, report, environment, or screenshot drift makes the evidence stale. `approve-html-deck` refuses to proceed until this evidence is complete and current.

After application, all section HTML approvals are pending again. Whole-deck approval records the current per-section DOM, referenced registry/provenance closure, asset hashes, and global CSS/theme digests, then conversion revalidates them. Generated, authoring, and final artifacts are never changed by the proposal workflow.

## Compatibility

`rolling_sections` remains available for migrated or explicitly incremental work. It retains its separate HTML promotion and Bento acceptance gates. Existing schema v2 states without an `authoring.strategy` field are interpreted as rolling for compatibility; new working decks should declare `whole_deck` explicitly.
