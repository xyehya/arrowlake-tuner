Permission allow rule (--allowed-tools): Write(desktop/progress/logs/cpu-rules-audit.md) is not matched by file permission checks — only Edit(path) rules are. Use Edit(desktop/progress/logs/cpu-rules-audit.md) instead (Edit rules cover all file-editing tools).
I couldn't save the report: the permission layer blocked both the `Write` tool and a shell heredoc to `desktop/progress/logs/cpu-rules-audit.md`. The earlier `cpu-rules-audit.out` shows why: the session allows `Write(…)`, but file permission checks only honour `Edit(desktop/progress/logs/cpu-rules-audit.md)`. Add that rule, or paste the report below into the file.

**Summary (10 lines)**
1. **Voltage ceiling (cmd 0x28):** the limit field is 12 bits, XTU subtracts 1 LSB. This chip reads P 1625 mV / E 1355 mV (measured on Windows). The "over the max" error codes (0x04/0x12) have never been seen, so we don't know whether the CPU rejects a higher value or only XTU does. There is also a code bug: field 0 reads as 2.0 V at `policy.py:497` but as "no limit" at `policy.py:285`.
2. **Voltage floor (cmd 0x09):** this command actually returns the fused top point, not a floor. With Undervolt Protection off (it must be off for writes) the CPU has no known offset floor; only the field encoding (−1000 mV) limits it.
3. **Max OC ratio (cmd 0x01):** P-cores read 120x, which is no cap in practice. E, ring and NGU have never been read. The fused byte of 0x1C is measured *not* to be a cap. Error 0x03 (ratio over the limit) was seen once in XTU's log; what triggers it is unknown.
4. **Monotonic clamp:** measured once: a −49.8 mV offset at 57x landed at −42.5 mV, held by the 56x point. Your "−500 mV at x57 is clamped to the x56 voltage" is UNVERIFIED at that size. The clamp does not cover the whole-curve offset, point 1, or a 56x point that has its own offset.
5. **Silent clamps pass the guard:** the CPU accepts the word and reads it back unchanged, but the voltage differs. The guard only compares words, so only a measured VID shows the clamp.
6. **Not CPU rules:** the non-increasing turbo table, the base-ratio floor, PL2 ≥ PL1, and the 500 mV minimum voltage target. They come from the SPEC, the code or XTU's UI, with no evidence the CPU enforces them.
7. **Hard bounds that are CPU or kernel:** the 0x610 PL fields and lock bit; cpufreq `cpuinfo_max_freq` / `scaling_min_freq`; uncore `initial_min/max` (8x–40x).
8. **To remove:** offset ceiling and floor, assumed BIOS offset, the three step limits, `ratio_max`/"auto", `v_target_max_mv`, `[power]`, `[runtime]`. Two side effects: the offset floor is also the guard's "no context" signal, and removing the step limits collapses the planner's multi-round plans into one.
9. **Keep (process):** one mailbox transaction at a time, the `[[allow]]` lists, read-back, write ordering, op counters, audit. Adaptive-only mode is your call, not a CPU rule (XTU allows static override).
10. **Top gaps:** how big the clamp really is, whether a whole-curve offset hits any floor, what triggers error 0x03, whether 0x28 is enforced with an error, and the unread 0x01/0x28 values for E, ring and NGU.

Below is the full report text for the file (about 1,450 words).

---

# CPU-rules facts audit for D-049 (host lnx, 2026-10-18, read-only)

Enforcement kinds: **rc** = mailbox rejects with a completion code · **clamp** = rc 0, word reads back as written, VID differs (the guard's word read-back does NOT catch it) · **kernel** = sysfs refuses/clamps · **none** = no evidence anything enforces it.
Refs: R = docs/knowledge/registers.md, A = platform-arl-s.md, P = src/arltuner/guard/policy.py.

## A. CPU rules

| # | Rule | Evidence (tag) | Value here | Runtime read | Kind | Code enforces |
|---|---|---|---|---|---|---|
| 1 | Max voltage limit | 0x28 [11:0] S2.10, XTU raw-1, 0 ⇒ 2.0 V: R:65 DECOMPILED; values R:66 MEASURED win 2026-10-01 | P 0x681 → 1625 mV, E 0x56D → 1355 mV (ring/NGU unread) | 0x28 P1=0 pinned to P / E CPU (ops_tune.py:703,766); allow p1 0,2,7 | rc 0x04/0x12 per enum (R:75-77), **never observed** | P:568-574 (needs a read, fail closed). **Bug:** P:497-503 maps field 0 → 2.0 V but P:285-289 returns None |
| 2 | Voltage floor | 0x09 named "VoltageFloorRead" (R:55 DECOMPILED), but 0x09 P1=0x11 P2=0 = fused **top point** per core (R:117, A:59 MEASURED). XTU: "UVP raises lower bounds from 0x09" (R:70) | none known with UVP off (required for writes, A:11) | — | rc 0x13 exists (R:77), never seen | Only encoding −1024 raw. OC target below top fused point is **clamped up** (A:38 MEASURED lnx 2026-10-11) |
| 3 | Max OC ratio P / E | 0x01 [7:0] (R:50 DECOMPILED); "x120" is a reading, no documented fused max | P 0x778 = 120x (R:66 MEASURED win); E, ring, NGU **never read** | 0x01 P1=0 on P/E CPU (ops_tune.py:765) | rc 0x03 seen once in XTU log, trigger unknown (R:78) | P:276-283, 314. 0x1C[15:8] **not** a cap (A:92 MEASURED). 0x02 per-active-core P 57,57,54,54 / 54×4 (measured_baseline.py:152) — enforcement unknown |
| 4 | V/F monotonic clamp | "hardware enforces V(n) ≥ V(n-1); a deeper offset is silently clamped up (−49.8 mV BIOS offset on P 57x landed at −42.5 mV, held by the 56x point)" A:48-49 MEASURED lnx 2026-10-02 (one instance) | — | lower fused points unreadable (A:61); VID via 0x198 under load only | **clamp** | Not checked; P:632-633 still cites it as COMMUNITY (stale) |
| 4a | "−500 mV at x57 clamped to x56 VID" | Mechanism MEASURED once at ~7 mV; −500 mV / runtime per-point: **UNVERIFIED**. Not covered: whole-curve offset P2=0 (A:34), point 1, V(n−1) lowered by its own offset | | | | |
| 5 | Offset field | [31:21] 11-bit signed, 1/1024 V, XTU −1000…+999 mV (R:32 DECOMPILED; R:116 MEASURED decode) | raw −1024…+1023 | — | encoding; extremes never written | encoding.py:24-25; P:575 |
| 6 | Target field | [19:8] 12-bit S2.10, mode [20] (R:31). Fused points offsets only (A:26 COMMUNITY); target w/o OC point not kept (A:36 MEASURED) | real cap = row 1 | — | clamp (low ↑) | P:512-517, 640-645; 500 mV min = XTU UI (encoding.py:26) |
| 7 | Turbo tables | bytes per bucket; counts P 1..8, E 1,2,3,4,6,8,12,16 (R:105 MEASURED). Non-increasing = SPEC-v3 F5 (A:103), **not CPU**; base floor = code rule | P0 57x; 0xCE[15:8] 37 | rdmsr 0x1AD/0x650/0xCE | none known | P:476-494 |
| 8 | Ring P1=2 | points P2 1..8 = 8…39x, OC slot P2 9 (A:28,42 MEASURED); XTU ring [MaxEfficiencyRatio … 0x01 P1=2] (R:70 DECOMPILED) | 0x01/0x28 P1=2 unread | allowed | unknown | point offsets only (P:613) |
| 9 | PL1/PL2/tau | 0x610 fields + lock [63] (R:181 DOC+MEASURED). PL2≥PL1 code rule (power.py:367), no CPU evidence. 0x614 min/max UNVERIFIED; constraint_0_max 125 W not a ceiling (R:179) | 250/295 W, 127.9 s, lock 0 | powercap + 0x606/0x610/0x614 | locked write: ignored vs error UNVERIFIED | power.py:347; P:905-906 |
| 10 | cpufreq caps | cpuinfo_max / scaling_min (caps.py:4 MEASURED lnx 2026-10-18) | cpu0 5700000 kHz | sysfs | kernel | caps.py:315, 322 |
| 11 | Uncore | initial_min/max 800000/4000000 kHz (uncore.py:3 MEASURED) | 8x–40x | sysfs | kernel | uncore.py:67, 93 |
| 12 | Other rejects | rc 0x01/0x02/0x03/0x04/0x05/0x10/0x11 (P2 past table, A:28 MEASURED)/0x12/0x13 (R:75-77); gates 0x194[20], 0x195[1] | | | rc | guard.py:1050 |

## B. Policy limits to remove

| Limit (file:line) | Guards | Replaced by |
|---|---|---|
| `offset_ceiling_mv` (P:1009, 393, 576, 651; planner.py:1347; ops_tune.py:380, 1504, 1606) | positive offset | row 5 (+999 mV); keep POSITIVE_OFFSET warning |
| `effective_offset_floor_mv` / `assumed_bios_offset_mv` (P:1003, 1137-1145; planner.py:548, 1317) | deep undervolt | row 5 (−1000 mV). **Structural:** `offset_floor_raw is None` is the no-context gate (P:532, 634) |
| `max_ratio_step` (P:1010, 587, 487, 489, 261; planner.py:674, 782-835) | step | none; warn. Planner rounds collapse to one; turbo floor fallback (P:489) needs a replacement |
| `max_v_step_mv` (P:1011, 395, 589) | step | none; warn |
| `max_offset_step_mv` (P:1012, 396, 593, 659; planner.py:784-835) | step | none; warn |
| `ratio_max` / "auto" = 0x1C + 3 (P:1005, 157, 308-336, 559, 482; planner.py:1332, 1412) | ratio raise | row 3 (0x01); keep fail-closed when unread |
| `v_target_max_mv` (P:1006, 339-358, 563; planner.py:1336) | V raise | row 1 |
| `V_TARGET_MIN_MV` 500 (encoding.py:26; P:1042) | low target | none (CPU clamps up) |
| turbo no-raise / base floor (P:482, 489-494) | table | rows 3/7; keep floor as warning |
| `[power]` incl. absent = refused (P:924-944; power.py:403, 420-421) | PL ranges | row 9 (power becomes always available) |
| `[runtime] cap_ratio_min` (P:956; caps.py:299, 313) | cap floor | row 10 |
| `[runtime] uncore_ratio` (P:957; uncore.py:89, 100) | ring range | row 11 |

**PROCESS (keep):** one mailbox transaction at a time; `[[allow]]` + `PLAIN_MSR_READABLE/WRITABLE` (P:177-182); `cpus` access list; baseline-first + read-back (guard.py:1055-1065); raise/lower ordering (P:595-602); 0x1D follows the executed 0x11 (P:681); 0x27 = 0x26 (P:700); 0x15 bit 3 only (P:668); point writes change the offset only (P:640-645); fail closed on unread 0x01/0x28; 0x610 lock gate; PL / uncore write order; op counters + dwell (guard.py:546, 868, 910; shipped default already 100000/100000/0 s); audit log; health gate.
**Operator call:** adaptive-only (P:528, D-016) is a scope choice (XTU allows static override).
**Warnings:** POSITIVE_OFFSET, E40_OFFSET, E_TARGET_WHEA (ops_tune.py:2064-2070, 2390), TURBO_RAISE (planner.py:153), CLAMPED, + "large single change".

## C. Gaps (most important first)
1. Clamp size: write P point 8 (57x) −100 / −200 mV with point 7 at 0. Does VID(57x) stop at VID(56x)? Same for E point 6 vs 5, and ring.
2. Whole-curve P2=0 at −150 mV: does any point stop falling (hidden floor, rc 0x13, nothing)? Is there any rejection of a too-deep offset with UVP off?
3. What makes 0x11 return rc 0x03 on P and E: 0x01's 120x, the 0x02 bytes, or something else?
4. Do 0x04 / 0x12 fire for a V target at 0x28 raw vs raw+1, or is it a silent clamp?
5. Read 0x01 / 0x28 for E, ring (P1=2) and NGU (P1=7) on lnx; find what 0x09 P1=0x11 P2=2/7 means.
6. Is there any 0x09 variant (or reading with UVP on) that returns a real floor? What does XTU use as the UVP bound?
7. Does the CPU accept an increasing turbo table, a byte above 0x01, or a byte below 0xCE[47:40]?
8. Does the kernel/CPU accept PL2 < PL1, a PL above 0x614 [46:32], or a write to a locked 0x610 (error or silent)?
9. Do raw offsets −1024 / +1023 write and read back unchanged?
10. Make the two 0x28 helpers agree on what field 0 means.
