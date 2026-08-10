# Legacy compatibility aliases

Natural conversation is the primary BentoSlide UX. The phrases below remain supported only so existing instructions and demonstrations keep working; users do not need to memorize them.

| Alias | Compatibility behavior |
| --- | --- |
| `この資料を作成して` | Read `REQUEST.md`, resolve the primary source, create planning artifacts, register sections, submit the plan, and stop for material approval. |
| `この方針で進めて` | Record plan approval, author the complete single HTML/registry deck, open whole-deck preview, and stop for visual/story approval. |
| `次へ` | At a displayed change proposal, confirm and apply that exact candidate; otherwise approve only the displayed whole-deck checkpoint. Never ask for an internal slide/section ID. |
| `BentoSlideに変換して` | Validate all approval evidence, perform a full deterministic build, initialize authoring artifacts, and stop before opening the editor or creating final artifacts. |
| `BentoSlideで編集を開始して` | Open the revision-checked shared authoring editor without crossing a content-approval gate. |
| `内容を確定して` | Validate the authoring document and registry, enter content review, and request approval without recording it automatically. |
| `この内容で最終調整へ` | Treat the currently displayed content as explicitly approved, bind approval to both revisions, and initialize final artifacts plus immutable baselines. If an older final exists after a deliberate content revision, archive its complete final/baseline set before the transactional restart. |
| `最終調整を開始して` | Open presentation-only final editing, then save, reload, validate, obtain final approval, and complete. |

All aliases obey the same whole-deck change review, revision, registry, transaction, writer-lease, generated/final protection, and human-approval gates as their natural-language equivalents. Explicit `rolling_sections` projects retain their section approval/acceptance gates.
