# G-140 — public scrub: no references to private repos, personal paths or the earlier tool's name

- **Status:** done 2026-10-12 (pending merge) · **Host:** lnx · **Tier:** T0 · **Branch:** `task/G-140-public-scrub` · **Decision:** D-041

## Goal (operator 2026-10-12)
The repo goes public. A reader must never meet a name or path that points outside it (HEAD only; no history rewrite).

## Done
1. **Per-user paths in code:** audit log `$XDG_STATE_HOME/arltuner/audit.jsonl` (default `~/.local/state/arltuner/`), `%LOCALAPPDATA%\arltuner\audit.jsonl`,
   dev lock `/run/lock/arltuner-mailbox.lock` (else the temp dir); the earlier names and the fallback code are gone (`cli.py`, `helper/main.py`, `install.sh`,
   `tools/win/*`, `tools/lnx/*`, tests). The package is named `arltuner`.
2. **Private monitoring repos:** code comments cite `docs/knowledge/` instead; the kernel `turbostat` tool stays where it means the tool.
3. **Citations of the earlier research** in docs became neutral wording (evidence tags kept); `docs/PROVENANCE.md` deleted; AGENTS.md says workers use only this repository.
4. **Personal paths** became `~`, `$HOME`, `%USERPROFILE%`; "private GitHub" wording became the public URL.
5. **Interoperability disclaimer** added to README; tool-procedure lines removed from `docs/HARDWARE.md`.
6. **Check:** a case-insensitive `git grep` for the earlier tool's name, the private monitor repo paths, the home directory and the session scratch path returns only
   the single D-041 sentence in `docs/DECISIONS.md` (plus `docs/guide/**` and `.claude/agents/documenter.md`, owned elsewhere).

## Owns
Everything except `web/*` (G-139), `docs/guide/**` (documenter), `.claude/agents/documenter.md` (orchestrator).

## Acceptance
`python3 tools/run_tests.py` green; the check in 6 clean.
