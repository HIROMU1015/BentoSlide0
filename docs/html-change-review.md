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
6. Only explicit confirmation approves and applies it. Every affected slide that remains in the deck is then browser-checked together. Whole-deck approval is blocked until that evidence is current.

The explanation uses slide titles. IDs, revisions, paths, and CLI commands remain internal unless diagnostics are requested.

## Impact rules

Each `bento/html-change-proposal/v2` records requested, related, changed, affected, added, and removed slides; slide order and section-membership changes; registry and global-style changes; affected sections; and readable summaries.

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

The proposal digest binds the four revisions, candidate paths, user request, readable summary, impact summary, and every machine-computed impact field. Approval records that exact digest and recomputes the complete impact evidence. Drift or tampering in either the candidate, explanation, or impact requires a fresh proposal.

Application acquires one union writer lease over canonical HTML/registry, candidate HTML/registry, state, and report. It re-reads and validates all four revisions plus the approved proposal digest while that lease is held, then uses the repository artifact transaction layer to replace canonical HTML and registry together with state/report updates. Cancellation never applies candidate bytes and remains available even when a proposal has gone stale.

Application creates a pending post-apply review bound to the installed HTML/registry revisions and proposal digest. `check-html-change` renders the installed deck with the deterministic browser harness, rejects blocked network activity, checks layout bounds and content overflow, and transactionally stores the browser report, environment fingerprint, and one screenshot for every affected slide still present. Removed slides remain listed in proposal impact but cannot have a post-apply screenshot. Any later HTML, registry, report, environment, or screenshot drift makes the evidence stale. `approve-html-deck` refuses to proceed until this evidence is complete and current.

After application, all section HTML approvals are pending again. Whole-deck approval records the current per-section DOM, referenced registry/provenance closure, asset hashes, and global CSS/theme digests, then conversion revalidates them. Generated, authoring, and final artifacts are never changed by the proposal workflow.

## Compatibility

`rolling_sections` remains available for migrated or explicitly incremental work. It retains its separate HTML promotion and Bento acceptance gates. Existing schema v2 states without an `authoring.strategy` field are interpreted as rolling for compatibility; new working decks should declare `whole_deck` explicitly.
