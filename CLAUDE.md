@AGENTS.md

**On host `win` (Windows), task first:** read `captures/win/accepted-20261020-p58-e47-2/WINDOWS-RESTORE-NOTE.md` and follow it — restore the accepted
state good-p58-e47-2 (P undervolt + P0 58x OC, E 47x OC EC0 1.097 / EC1-3 1.077 V, turbo P 58,58,55x6 / E 47x8; supersedes the offsets-only state). Every hardware
step needs the operator's go in chat.

**Resume: read `docs/START-PROMPT.md` first** (both OSes). **You are the orchestrator: follow `docs/ORCHESTRATION.md`** — Sonnet workers code and test, you
review, merge, decide, and keep your context clean; use the `advisor` tool at the points it lists.

## Claude Code specifics
- The **orchestrator** is the top-level interactive session. Workers are `Agent` calls (model `sonnet`, prefer `isolation: "worktree"`);
  they follow AGENTS.md and never write hardware.
- `.claude/settings.json` puts hardware-write-capable commands on `ask` — a speed bump; the rule is AGENTS.md / `docs/SAFETY.md`.
- Research: background agents return a compact tagged report; the orchestrator files it under `docs/knowledge/` (curated, never raw output).
