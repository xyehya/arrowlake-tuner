# G-154 — The ring (cache, P1 = 2) is part of the read, the accepted state and the restore

- **Status:** done (merged 2026-10-15)
- **Host:** lnx
- **Safety tier:** T0 (worker: simulator + fake sysfs only)
- **Branch/worktree:** `task/G-154-ring-accepted`
- **Depends on:** G-151 (merged), D-045

## Goal
Operator 2026-10-15: "set the cache state also as accepted in the profile" and "make sure the direct restore tuning script that doesnt rely on the gui is
also updated for the last state so a restore is always a command away". Live now (runtime, CLI `point-offset`): ring point 8 (39×) `0xFCC00027` and
ring point 9 (40×, the slot the BIOS ring OC ratio 40 creates) `0xFCC00028`, both −25.39 mV (raw −26); ring OC word (P2 = 0) `0x00000028` (BIOS-owned:
ratio 40, V target 0). Measured: an offset on P2 = 9 moves 40×, an offset on P2 = 8 does not move 40× (`docs/knowledge/platform-arl-s.md` §3,
`captures/lnx/ring-pl2-20261015.txt`). Today `tune.read`, the complete profile, `EXPECTED-words.txt`, the planner, `compare-accepted` and both restore
scripts know only P-cores, E-clusters and the turbo tables, so the ring is lost on every reboot and cannot be accepted.

## Fixed decisions (D-045; do not re-decide; report if one is impossible)
1. **Ring = one more unit, id `ring`** (read on CPU 0, P1 = 2). Its words: OC word (P2 = 0) + points 1..n, walked like the core tables (P2 = 1.. until rc
   `0x11`, at most 10). The OC slot is **not** special for the ring: every ring point 1..n (the slot P2 = 9 included) is an offset-only point with
   `expect_ratio` = its read ratio, written by the existing point path (`0x11` P1 = 2 P2 = k, read-modify-write of the offset field `[31:21]` only,
   read-back). This differs from the E OC slot (G-151: derived, never written) — state why in the code where the two meet.
2. **The ring OC word is BIOS-owned: recorded and compared, never written** (no policy entry writes `0x11 P1=2 P2=0`, and none is added). On restore,
   if the live ring OC word differs from the accepted one (BIOS ring ratio changed) or a ring point's ratio differs from `expect_ratio`, the **ring
   unit alone** is refused with a named check (`ring_oc_differs` / the existing expect-ratio check), the P/E units are still restored, and the compare
   reports the ring BAD (so the scripts exit non-zero).
3. **Complete profile:** a ring section holding the OC word as read (compare only) and the offset of every ring point (whole mV, `expect_ratio`; the
   G-148 `NOT_REPRESENTABLE` rule applies). Planning a complete profile writes only ring points that differ; G-151's per-unit order rule applies to
   the ring as its own unit (raising first). A profile without a ring section (every older accepted state / saved profile) never reads-for-write or
   writes the ring and is still valid.
4. **`EXPECTED-words.txt`:** one extra line `ring  <OC word> <point 1> ... <point n>` (hex, no 0x, same shape as a core line; key literally `ring`).
   Every parser (`compare-accepted`, `accepted-migrate`, `profiles/lastread.py` / `accepted.py`, `restore_tuning.sh`, `restore_tuning.ps1`) accepts it;
   a file without it = no ring in the accepted state (nothing about the ring is compared).
5. **`tune.read`** also reads the ring (one guarded read per word, the existing allowlisted `0x10 P1=2` entries; add its op count to the read's budget)
   and returns it (`ring: {oc, points}` in the shape of a unit); `accepted_match` gets a `ring` entry when the accepted state has one. `profiles.accept
   {from: "read"}` and `profiles.save` record it. `vf-dump` (CLI, used by both restore scripts for the read-back) dumps the ring too (JSON + text).
6. **Policy:** the installed operator policy already allows ring writes P2 1..9 (operator yes 2026-10-15); never touch `/etc`. The shipped
   `policies/default.toml`: make its ring entries (read P2 1..10, write P2 1..10, `vf_point_offset_bounded`) match what this feature needs, if it has
   core point writes; `install.sh` keeps never overwriting an existing policy. A ring write the policy refuses is a ring-unit refusal with the guard's
   own reason, never a silent skip.
7. **Simulator:** the reference ring model = OC word `0x28`, points 8/14/20/28/34/37/38/39× + P2 = 9 = 40× (present while the OC ratio is 40), rc `0x11`
   past it, offsets 0 at cold boot (replace the old "point 8 starts at raw −77" fixture where tests allow; keep any test that needs it explicit).
8. **Windows `restore_tuning.ps1`:** must parse the ring line without failing. Restore + compare the ring there the same way if it fits the script's
   structure (static checks only, no pwsh here — say so); otherwise print one line "ring not restored on Windows (G-154)" and report it.
9. **GUI:** no new controls. The pages must not break on the new `ring` unit / `accepted_match` entry (browser tests); show the ring in the Restore /
   Review step list and in the Read summary only as far as the existing renderers do it generically.

## Read first (only these)
- `src/arltuner/profiles/` (schema, complete, accepted, lastread, migrate, serialize), `src/arltuner/controls/planner.py`, `controls/order.py`,
  `controls/residual.py`, `src/arltuner/helper/ops_tune.py` (read, plan, restore, `accepted_match`), `helper/ops_accept.py`
- `src/arltuner/cli.py` (`point-offset`, `vf-dump`, `apply`, `compare-accepted`), `src/arltuner/msr/sim.py` (ring model), `policies/default.toml`
- `tools/lnx/restore_tuning.sh`, `tools/win/restore_tuning.ps1`, `docs/knowledge/platform-arl-s.md` §3, `docs/knowledge/registers.md` (domain P1 values)

## Owns
`src/arltuner/profiles/`, `src/arltuner/controls/`, `src/arltuner/helper/ops_tune.py`, `helper/ops_accept.py`, `src/arltuner/cli.py`,
`src/arltuner/msr/sim.py`, `policies/default.toml`, `tools/lnx/restore_tuning.sh`, `tools/win/restore_tuning.ps1`, `web/` only if a renderer breaks,
tests for all of these. Not `docs/guide/`, STATE, DECISIONS, knowledge, examples.

## Acceptance
- `ARLTUNER_BROWSER_TESTS=1 python3 tools/run_tests.py` green (2941 on main).
- Simulator flows with the live words above: (a) **cold boot** (ring offsets 0, OC word `0x28`, P/E stock) → Restore accepted (helper) and CLI `apply` of
  the accepted profile → ring P2 8 and 9 written `0xFCC00027` / `0xFCC00028` with read-back, P/E as before, compare OK incl. the ring line; (b) warm
  boot, all held → 0 writes, OK; (c) ring OC word changed (e.g. `0x27`, no P2 = 9) → ring refused by name, P/E restored, compare BAD for the ring only;
  (d) an accepted state without a ring line → no ring write, compare OK; (e) `profiles.accept {from: "read"}` writes the ring line and the ring section,
  and a re-read compares equal.
- `restore_tuning.sh --check` and apply paths exercised against the simulator where the existing tests do so; the ring appears in the capture.
- No zero ratio, no ring OC word write, no guard/policy change in `/etc`.

## Non-goals
No hardware. No ring GUI editor, no ring ratio change, no NGU/SA/GT. No power limits.

## Report-back (≤ 300 words)
Where the ring lives in each layer, the Windows decision, test count line, anything not done as fixed.
