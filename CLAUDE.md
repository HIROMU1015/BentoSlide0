# Claude Code entry

@AGENTS.md

## Claude Code notes

- The repository docs name two roles: Work (ChatGPT Work: source reading, planning, HTML authoring, visual review, Bento content editing) and Codex (state transitions, conversion, diagnostics, browser evidence, recovery). Claude Code may perform both. The `owner` and `handoff` fields in `deck.yaml` are workflow labels, not agent checks; change state only through `scripts.deck_workflow` as AGENTS.md requires.
- The detailed workflow procedure is the `bento-slide-build` skill. `.claude/skills/bento-slide-build/` points to the maintained copy in `.agents/skills/bento-slide-build/`.
- When a launcher or preview message says to open a URL in the ChatGPT Work browser, open the same localhost URL in Claude Code's built-in browser pane when one is available; otherwise give the user the URL. Stop servers with the matching `stop_*.cmd`.
- For a GitHub repository source, use `gh` or a connected GitHub MCP server only to inspect and select files; registration still goes through `capture-github-source` (see `docs/github-repository-sources.md`).
