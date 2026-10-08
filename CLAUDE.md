@AGENTS.md

**Resume: read `docs/START-PROMPT.md` first** (both OSes).

## Claude Code specifics
- The **orchestrator** is the top-level interactive session. Workers are `Agent` calls (model `sonnet`, prefer `isolation: "worktree"`);
  they follow AGENTS.md and never write hardware.
- `.claude/settings.json` puts hardware-write-capable commands on `ask` — a speed bump; the rule is AGENTS.md / `docs/SAFETY.md`.
- Research: background agents return a compact tagged report; the orchestrator files it under `docs/knowledge/` (curated, never raw output).
