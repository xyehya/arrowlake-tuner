# Apply latency — where the time goes (host: lnx)

## Measured 2026-10-18 `[MEASURED lnx 2026-10-18]`
Source: `audit.tail n=200` over the helper socket (read-only: `preflight` reads files only, `audit.tail` reads the log; no mailbox traffic).
Script: `desktop/progress/logs/measure_latency.py`. Window: 100 transactions of the 2026-10-18 07:52:57–07:53:17 boot run.

| Interval | median | p90 | max |
|---|---|---|---|
| `attempt` -> `result` (the mailbox transaction incl. 0x150 polling) | 1.0 ms | 1.0 ms | 3.0 ms |
| `result` -> next `attempt` (guard + executor overhead between transactions) | 194 ms | 197 ms | 199 ms |

- ~99.5 % of apply/read wall time is overhead between transactions, not the CPU. A 353-op restore ~ 69 s; a ~130-op `tune.read` ~ 25 s.
- `/var/lib/arltuner/audit.jsonl` = 15,938,180 bytes (root 0600) `[MEASURED lnx 2026-10-18]`. The guard's `read_state` re-parses the whole log
  per operation, twice (before and after taking the lock) — `guard/guard.py:19-34,954-975`; the cost grows with history.
- Other per-transaction costs (structural, not yet timed individually): `O_EXCL` lock file create+unlink, 2-3 fsync'd audit records, two health
  snapshots (taint, PCI listing, `/proc/modules`, thermal zones, MCE banks), `_Pacer` dwell + 50 ms (`controls/executor.py:48-60`).
- Hypothesis `[UNVERIFIED]`: the full-log parse dominates (16 MB JSONL x2 per op ~ 150-190 ms in CPython); the 50 ms pacer slack is the next item.
  Task G-211 times each part in the simulator with a 16 MB log before changing anything.

## Rule kept
One transaction at a time, the health bracket around each transaction, read-back of every write and the plan nonce are not negotiable (SAFETY.md,
D-032). Speed comes only from making the bookkeeping cheaper.
