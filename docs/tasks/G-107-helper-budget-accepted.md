# G-107 — helper: budget in heartbeat, accepted-state compare
- **Status:** in progress · **Host:** lnx · **Tier:** T0 (simulator) · **Depends on:** G-102

## Goal
Contract 8 addendum (G-107, D-030): `heartbeat {state, ops_used, ops_budget}`; `vf.dump` result `accepted_match` per core vs the trusted
`/etc/arltuner/accepted` EXPECTED words (trust-checked like every root file), `null` if absent/untrusted.

## Read first
`docs/ARCHITECTURE.md` contract 8 + addenda · `src/arltuner/helper/` · `profiles/EXPECTED-words.txt` (format) · `src/arltuner/vfdump.py` (`to_json`).

## Owns
`src/arltuner/helper/ops.py`, `src/arltuner/helper/core.py`, `tests/helper/` (not `helper/config.py`: G-106 owns it).

## Acceptance
Simulator tests: heartbeat counts match the audit log; budget from policy; accepted match / mismatch / absent / untrusted (symlink, foreign owner) ⇒
`null`; existing tests green.
