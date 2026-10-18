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

## After G-211 (merged 2026-10-18) `[MEASURED lnx 2026-10-18, simulator bench]`
`tools/bench_guard.py` (300 ops, 16.0 MB synthetic log, real fsync, simulator transport): guard overhead per op **211.8 ms -> 3.15 ms median
(p90 3.57 ms)**. Audit-state read 206.9 -> 0.07 ms (incremental parse: append-only, rescan on inode / size / anchor bytes / same-size mtime change);
health snapshots 3.5 -> 1.5 ms (fake tree, indicative). Lock 0.05 ms and fsync ~1 ms unchanged. The 194 ms live gap was the audit parse.
First scan per helper process still reads the whole log once (~100 ms). Live confirmation = hardware sitting H2 (needs `sudo bash install.sh` +
helper restart, then one timed `tune.read`). The full test suite went from ~405 s to ~222 s as a side effect.

## Live after G-211 (H2, 2026-10-18) `[MEASURED lnx 2026-10-18]`
Installed (`install.sh`, 21/21 self-checks), fresh socket-activated helper. `captures/lnx/desktop-h2-20261018.txt`, `desktop-h2-restore-20261018.txt`.
| Operation | Before (live) | After (live) |
|---|---|---|
| gap between transactions (median / p90) | 194 / 197 ms | **5 / 5 ms** |
| `tune.read` (155 ops) | ~30 s | **1.06 s** |
| restore accepted after a reboot (311 ops, 6 writes, 6/6 read back) | ~70 s (353-op restore) | **1.85 s** |
MCE 0, taint unchanged (12292 = CPU_OUT_OF_SPEC + OOT/unsigned NVIDIA module). Accepted match 13/13 afterwards.
