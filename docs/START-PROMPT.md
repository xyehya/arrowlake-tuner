# START PROMPT — resume arrowlake-tuner (Linux or Windows)

_Operator: open Claude Code in `~/AI/arrowlake-tuner` (Windows: `C:\Users\yk\AI\arrowlake-tuner`, elevated) and say "read docs/START-PROMPT.md"._

You are the **orchestrator** for arrowlake-tuner on the operator's machine (yk; Core Ultra 9 285K, ASRock Z890 Steel Legend WiFi, RTX 5080).
The repo is your memory: **`docs/STATE.md` beats any recalled memory.** The predecessor `~/AI/linuxmon` is a read-only archive — go there only
when `docs/PROVENANCE.md` sends you for raw evidence.

**How you run the project: `docs/ORCHESTRATION.md`** — autonomous A-to-Z orchestration, Sonnet workers (`model: "sonnet"`, worktrees) for
all coding and test runs, read-only Sonnet reviewers for security-relevant work, the `advisor` tool at the listed points, operator only for §4 items.

## 1. Start (every session)
1. `git pull` (if a remote exists, STATE) · `git log --oneline -5` · `python3 tools/run_tests.py` (Windows `py -3`) — expect OK.
2. Read `AGENTS.md` → `docs/ORCHESTRATION.md` → `docs/STATE.md` → (task work) `docs/tasks/INDEX.md` + the task file → `docs/SPEC.md` sections it cites.
3. Report in ≤ 10 lines: state, open decisions, what you start next — then **start it** (no go-ahead needed for T0 work: task files, workers,
   reviews, merges). Wait for the operator only for ORCHESTRATION §4 items (hardware access, limits, downloads, scope).

## 2. After a boot (operator asks "restore tuning")
- **Linux:** `bash tools/lnx/restore_tuning.sh` (operator types CONFIRM once) or, after a go-ahead in chat, `--orchestrated`. Expect
  "RESTORED: all cores/clusters match the accepted state." Any BAD line or refusal ⇒ stop and report; never retry blindly.
- **Windows:** elevated `powershell -ExecutionPolicy Bypass -File tools\win\restore_tuning.ps1` (`-Orchestrated` after a go-ahead). The ThrottleStop
  driver is carved from the operator's own exe (`docs/knowledge/windows-path.md`); XTU service stopped; driver stopped after applying.

## 3. Building (G-phases)
- Orchestrate: one task per Sonnet worker in a worktree (`isolation: "worktree"`, model `sonnet`), disjoint **Owns**; workers use the simulator and
  fake sysfs only — never hardware, never `~/AI/turbostat*`.
- Review every merged task (tests green, contracts in `docs/ARCHITECTURE.md` honoured); adversarial Sonnet review for security-relevant work (G-102,
  G-103, G-106).
- Hardware steps (first real helper read, install, gates) are orchestrator-only, each after an explicit go-ahead, per `docs/SAFETY.md`.

## 4. Session end
Commit (`G-NNN: …`, plus the Co-Authored-By line from the session reminder) · rewrite `docs/STATE.md` (≤ 80 lines) · decisions → `docs/DECISIONS.md`
· facts → `docs/knowledge/` · push.
