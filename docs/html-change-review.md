# Whole-deck HTML change review

The standard schema v2 workflow authors the complete fixed-size HTML/registry deck before conversion. Once that deck reaches HTML review, conversational corrections use review-before-apply rather than direct canonical edits.

## User experience

1. The user describes a concern in ordinary language.
2. Work/GPT identifies the requested slides and creates a temporary complete-deck candidate.
3. It compares current and candidate HTML/registry and explains:
   - what will change;
   - which slides changed directly;
   - which other slides should be reviewed because of narrative, structure, registry, or shared-style coupling;
   - which slides and global properties remain unchanged.
4. The preview offers both the current deck and candidate without replacing the current deck.
5. Work/GPT asks whether that exact proposal is acceptable.
6. Only explicit confirmation approves and applies it. The affected slides are then browser-checked together.

The explanation uses slide titles. IDs, revisions, paths, and CLI commands remain internal unless diagnostics are requested.

## Impact rules

Each `bento/html-change-proposal/v1` records requested, related, changed, affected, added, and removed slides; slide order and section-membership changes; registry and global-style changes; affected sections; and readable summaries.

- A slide-local DOM change reviews the requested and actually changed slides.
- An explicitly related narrative change reviews both the requested and related slides.
- A registry change reviews every slide in sections whose provenance/dependency digest changed.
- A slide addition, removal, reorder, section move, or shared CSS/theme change reviews the complete deck.

The computed affected list is conservative. The agent may add related slides based on narrative reasoning, but may not omit machine-detected changes.

## Revision and persistence contract

The proposal snapshots candidate HTML and registry next to the canonical HTML so local relative assets resolve in preview. A diagnostics report is stored under `output/html-change-proposals/`; neither snapshot nor report is a deliverable or part of generated Bento.

Confirmation is valid only while all four byte revisions remain unchanged:

- canonical HTML;
- canonical registry;
- candidate HTML;
- candidate registry.

Approval also recomputes the complete impact evidence. Drift or tampering requires a fresh proposal. Application uses the repository artifact transaction layer to replace canonical HTML and registry together with state/report updates. Cancellation never applies candidate bytes and remains available even when a proposal has gone stale.

After application, all section HTML approvals are pending again. Whole-deck approval records the current per-section DOM, referenced registry/provenance closure, asset hashes, and global CSS/theme digests, then conversion revalidates them. Generated, authoring, and final artifacts are never changed by the proposal workflow.

## Compatibility

`rolling_sections` remains available for migrated or explicitly incremental work. It retains its separate HTML promotion and Bento acceptance gates. Existing schema v2 states without an `authoring.strategy` field are interpreted as rolling for compatibility; new working decks should declare `whole_deck` explicitly.
