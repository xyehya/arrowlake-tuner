@AGENTS.md

**On host `win` (Windows), 2026-10-20 task first:** read `captures/win/accepted-offsets-20261020/WINDOWS-RESTORE-NOTE.md` and follow it — restore the
Linux-tested V/F point offsets only (turbo ratios and PL1/PL2 are set in the BIOS). Every hardware step needs the operator's go in chat.

**Resume: read `docs/START-PROMPT.md` first** (both OSes). **You are the orchestrator: follow `docs/ORCHESTRATION.md`** — Sonnet workers code and test, you
review, merge, decide, and keep your context clean; use the `advisor` tool at the points it lists.

## Claude Code specifics
- The **orchestrator** is the top-level interactive session. Workers are `Agent` calls (model `sonnet`, prefer `isolation: "worktree"`);
  they follow AGENTS.md and never write hardware.
- `.claude/settings.json` puts hardware-write-capable commands on `ask` — a speed bump; the rule is AGENTS.md / `docs/SAFETY.md`.
- Research: background agents return a compact tagged report; the orchestrator files it under `docs/knowledge/` (curated, never raw output).
