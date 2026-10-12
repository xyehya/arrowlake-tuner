# G-140 — public scrub: no references to private repos, personal paths or legacy names

- **Status:** in progress · **Host:** lnx · **Tier:** T0 · **Branch:** `task/G-140-public-scrub`

## Goal (operator 2026-10-12: "removing references to unobvious stuff like linuxmon which is another repo")
The repo goes public. A reader must never meet a name or path that points outside it. From the git-history review (HEAD only; no history rewrite):
1. **Legacy names in code** (`linuxmon`): the audit log `~/.local/state/linuxmon/audit.jsonl` / `%LOCALAPPDATA%\linuxmon\` and lock
   `/run/lock/linuxmon-mailbox.lock` (kept only to share the per-boot budget with the predecessor CLI — no longer needed; SPEC OD-1). Installed
   paths already are `/run/arltuner/mailbox.lock` + `/var/lib/arltuner/audit.jsonl` (D-030). Uninstalled/dev paths become
   `$XDG_STATE_HOME/arltuner/audit.jsonl` (default `~/.local/state/arltuner/`), `%LOCALAPPDATA%\arltuner\`, and lock under
   `$XDG_RUNTIME_DIR/arltuner/` (or what the code already uses for dev). Remove the legacy fallback code, update tests
   (`tests/install/test_paths.py`, `tests/test_cli*.py`), `src/arltuner/cli.py`, `helper/main.py`, `health/*`, tools/win scripts, `install.sh`
   messages. Record as **D-041** in docs/DECISIONS.md (append).
2. **`turbostat` / `turbostat+` repos** (`src/arltuner/health/*`, `platform/preflight.py`, tests, docs): the kernel's `turbostat` *tool* is a
   public Linux utility and may stay where it means the tool; references to the operator's private repos (`~/AI/turbostat`, `turbostat+`) go.
   Cite facts by their `docs/knowledge/` entry instead.
3. **`linuxmon` / predecessor citations in docs** (`docs/knowledge/*`, `DECISIONS.md`, `SPEC*.md`, `RUNBOOK-*`, `HARDWARE.md`, `SAFETY.md`,
   `ROADMAP.md`, `ARCHITECTURE.md`, `README.md`, `AGENTS.md`, `CLAUDE.md`, `START-PROMPT.md`, `ORCHESTRATION.md`, `STATE.md`, `docs/tasks/*`,
   `docs/review/*`): rewrite to neutral wording ("earlier research on the reference machine", "[MEASURED …]" stays). `linuxmon@<hash>:path`
   citations → drop the pointer, keep the fact + tag. `docs/PROVENANCE.md` → delete; one sentence in README "History" if needed. AGENTS.md /
   CLAUDE.md: drop the rules naming private repos (replace with "workers use only this repo"). Keep the meaning of every decision.
4. **Personal paths:** `/home/yk/...` → `~/...` or `$HOME`; `C:\Users\yk\...` → `%USERPROFILE%\...`; remove the `/tmp/claude-1000/...` session
   path in `docs/tasks/G-115-cpu-facts-ui.md`. "private GitHub URL" / "private repo" wording → the public URL `https://github.com/xyehya/arrowlake-tuner`.
   The handle "yk" as operator in tests/examples may stay (neutral), but prefer "operator" in prose.
5. **Decompile-derived notes** (`docs/knowledge/mailbox-recon-summary.md`, `windows-path.md`, `[DECOMPILED …]` tags, HARDWARE.md tool lines):
   keep the facts (interoperability), add one short disclaimer in README ("Register facts were established by measurement and by studying
   vendor tools for interoperability; no vendor code or binaries are included.") and remove tool-procedure lines (ILSpy/Ghidra/WinDbg
   file-copy steps) from HARDWARE.md.
6. Final check: `git grep -nI -i "linuxmon\|turbostat+\|AI/turbostat\|/home/yk\|Users\\\\yk\|claude-1000"` returns nothing outside
   docs/DECISIONS.md's D-041 sentence (which may name the old path once).

## Owns
Everything except `web/*` (G-139), `docs/guide/**` (documenter), `.claude/agents/documenter.md` (orchestrator).

## Acceptance
`python3 tools/run_tests.py` green; the grep in 6 clean.
