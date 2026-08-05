# Start here

This repository represents one local paper-slide project. Put the primary paper in `sources/private/`, optionally fill in `REQUEST.md`, then tell ChatGPT Work:

```text
この資料を作成して
```

Work and Codex must read `deck.yaml` themselves, follow the current stage in `workflow/WORKFLOW.md`, update state and concise logs, and select chapter filenames and commands without asking the user.

## Sources and roles

- Papers and supplementary files are discovered under `sources/`; an explicit `project.primarySource` in `deck.yaml` resolves ambiguity.
- ChatGPT Work owns paper understanding, explanation policy, story, chapter HTML/registry authoring, visual review, and final layout adjustment.
- Codex owns workflow implementation, HTML-first conversion, diagnostics, browser checks, launchers, and final technical validation.

The user approves three material decisions: the overall explanation/story/slide plan, each chapter's major visual composition, and the completed final Bento deck. Routine state updates, filenames, logs, registries, and commands are agent responsibilities.
