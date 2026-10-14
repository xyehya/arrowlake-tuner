# G-153 — Plan preview lists the apply's real write order (G-151); the GUI reconnects to a restarted helper

- **Status:** ready
- **Host:** lnx
- **Safety tier:** T0 (worker: simulator + fake helper only)
- **Branch/worktree:** `task/G-153-preview-order`
- **Depends on:** G-151 (merged)

## Goal
1. **Preview order.** `tune.plan` builds its step list with `ops_tune._predicted_steps`: a fixed order (0x15, all point writes, then the per-core triples, turbo)
   that does not run G-151's `controls.order.reorder_writes`. The apply (`build_plan` from fresh reads) does run it, so the Review dialog can now show a
   different order than the one that will be sent. Live 2026-10-14 (installed helper, last read present): intent EC1 point 6 −40 → −45 mV + OC target
   1.1123 → 1.1150 V; the preview listed point 6 first, while G-151's model says OC first (47× would dip −35.5 vs bound −34 raw). Make the preview
   use the same order as the apply wherever the last read gives the baselines (`_last_baseline` — today used only for complete profiles; use it
   for any intent when it covers the units), via the same `reorder_writes` (build the predicted `PlanStep`s with before/after words, or call the
   planner itself on the last-read baselines — pick the one that cannot drift; no duplicate ordering logic). Without a usable last read: keep the
   static list and add one note "the apply orders the writes so that no ratio's voltage dips (G-151); the order is decided from its own reads".
   A unit the order model refuses (`write_order`) is refused in the preview too (same text).
2. **Helper reconnect.** After `install.sh` restarts the helper (socket activation), the running GUI answers every call with 503
   `helper_unavailable` until the GUI server is restarted. The GUI's helper client should reconnect to `/run/arltuner/helper.sock` on the next call
   after a broken connection (once per call, no loop; the same peer checks as the first connect, G-142/D-042/D-043). Long-op state and the events
   stream recover the same way; an op in flight when the helper died stays failed (no replay).

## Read first (only these)
- `src/arltuner/helper/ops_tune.py` (`op_tune_plan`, `_predicted_steps`, `_last_baseline`), `src/arltuner/controls/order.py`, `controls/planner.py` (`build_plan` call of `reorder_writes`)
- `src/arltuner/web/server.py` (helper client, `helper_unavailable` paths), the helper client module it uses, D-042/D-043 in `docs/DECISIONS.md`
- tests: `tests/helper/test_write_order.py`, `tests/helper/test_tune.py` (plan preview), `tests/web/test_server_security.py` / `test_helper_socket.py`

## Owns
`src/arltuner/helper/ops_tune.py` (preview only), `src/arltuner/web/server.py` + the GUI-side helper client, tests for both. Not `controls/` (read-only use),
not `web/` JS unless the Review dialog needs the new note shown, not docs/guide, STATE, DECISIONS.

## Acceptance
- `ARLTUNER_BROWSER_TESTS=1 python3 tools/run_tests.py` green (2933 on main).
- Simulator: with the split state live and a last read, the preview of the intent above lists the EC1 OC triple before the point-6 writes, equal to the
  apply's own order (assert preview order == executed order); a pure undervolt keeps points-first; without a read the note is present.
- Fake helper: kill/restart the helper socket under a running GUI server → the next call succeeds after one reconnect; peer checks still enforced.

## Non-goals
No hardware. No change to the ordering rule itself, the guard, or what the apply sends.

## Report-back (≤ 250 words)
What changed per item, test count line, anything not done as described.
