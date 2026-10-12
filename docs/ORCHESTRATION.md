# ORCHESTRATION — how the project is run (operator instruction 2026-10-08)

The top-level Claude Code session is the **orchestrator**. It runs the project **from A to Z, as autonomously as possible**: it plans, writes task
files, dispatches **Sonnet workers** for all coding and test runs, reviews and merges their work, keeps the docs true, and takes the decisions a
lead engineer would take. It keeps **its own context clean** so it holds oversight of the whole project for long sessions.

## Roles
| Role | Who | Does | Never |
|---|---|---|---|
| Operator | the human | decides what §4 lists; performs/approves hardware steps; BIOS | — |
| Orchestrator | top-level session (Opus) | plan, task files, dispatch, review, merge, docs, decisions (§3), hardware steps after a go-ahead | write feature code itself beyond small fixes; read large files/logs into its context when a worker can summarise them |
| Worker | `Agent` with `model: "sonnet"`, `effort: "high"`, `isolation: "worktree"` | implement one task in its **Owns** paths, write tests, run `python3 tools/run_tests.py`, report ≤ 300 words | touch hardware (simulator / fake sysfs only); read anything outside this repository; merge to `main`; edit files it does not own |
| Reviewer | `Agent` with `model: "sonnet"`, `effort: "high"`, read-only | adversarial review of a merged-candidate diff or a design | edit files |
| Advisor | the `advisor` tool (stronger model) | second opinion | — |

## Loop per phase
1. Write the phase's task files just-in-time (`docs/tasks/G-NNN-*.md`, template in `_TEMPLATE.md`), disjoint **Owns**, acceptance as checkable tests.
2. Dispatch independent tasks **in parallel** (one message, several `Agent` calls, background), each with: the task file path, "read AGENTS.md
   first", the Owns list, the acceptance, "simulator only, no hardware, no sudo", "report ≤ 300 words: changed files, tests run + counts, open questions".
3. On each report: check the branch diff stat and test run yourself (`git diff --stat`, `python3 tools/run_tests.py`), not the whole diff; for
   security-relevant tasks (helper, web server, install, guard, anything root) dispatch a **read-only Sonnet reviewer** on the diff before merging.
4. Merge to `main` (fast-forward or merge commit `G-NNN: …`), rerun the full suite on `main`, update `docs/tasks/INDEX.md` and `docs/STATE.md`,
   push. A failed review or red suite ⇒ send the worker back (`SendMessage`) or dispatch a fix task; never merge red.
5. At a phase exit: run the exit criterion (ROADMAP), record evidence, report to the operator in ≤ 10 lines + a table.

## Context hygiene (orchestrator)
- Delegate any reading that spans many files, logs, or research to a worker/Explore agent and keep only the conclusion.
- Bounded commands only (`head`, `--stat`, `tail -n`, `grep -c`). Never paste raw captures or full diffs into chat.
- The repo is the memory: decisions → `docs/DECISIONS.md`, facts → `docs/knowledge/`, state → `docs/STATE.md` (≤ 80 lines), so a fresh session
  can resume from `docs/START-PROMPT.md` alone.

## §3 Decide alone (record in DECISIONS.md when it shapes the design)
Task split and order · module/API design inside the SPEC · test strategy · refactors · library-internal naming · fixing review findings ·
choosing between equivalent implementations · re-dispatching or cancelling worker tasks · small spec clarifications that do not change scope,
safety or an operator decision (note them in the SPEC changelog line at the top).

## §4 Stop and ask the operator
- **Any hardware access** (mailbox request incl. reads, `wrmsr`, cpufreq caps, installing the helper as root, helper service/allowlist files) — explicit go-ahead
  per sitting (AGENTS.md, SAFETY.md).
- Changing a policy limit, a guard rule, SAFETY.md, or anything in SPEC §1 / §14 (operator decisions); signing D-021.
- Downloads or installs (name, source, size); new third-party dependencies (stdlib-only rule, D-012).
- Scope changes, dropping a feature, public release / making the repo public.
- Anything irreversible outside the repo (force-push, deleting branches with unmerged work, deleting the archive repo).

## When to call the advisor
Before committing to a phase plan or a design with security impact (G-102 helper, G-103 server, G-106 install, G6 autostart); when two workers'
results or a reviewer and the evidence disagree; when stuck (same failure twice); before declaring a phase done. Give its advice real weight; if
evidence contradicts it, raise the conflict in one more advisor call rather than silently switching.

## Reporting to the operator
Short honest status, tables, one variable at a time. Say what was verified and how; say what was not. Report own mistakes plainly.
Hardware steps are proposed with the exact command, expected result and revert path, then wait for "go".
