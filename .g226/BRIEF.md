# G-226 test migration brief (read fully)

Worktree: /home/yk/AI/arrowlake-tuner/.claude/worktrees/G-226 (branch task/G-226-cpu-limits-only). Source changes are DONE and committed (`git show --stat HEAD`); your job is the TESTS.
Rules: NO hardware (never sudo, /dev/cpu, /sys writes, modprobe, systemctl, the installed helper, /run/arltuner), simulator + fake sysfs only. Python stdlib only.
Edit ONLY the test files assigned to you. Do NOT edit anything under src/, policies/, docs/, nor shared support files (tests/**/_support.py, tests/guard/fakes.py, tests/fakesys.py,
tests/helper/_measure_support.py) - if you need a change there or you think the SOURCE is wrong, say so in your report (what, where, why) and carry on. Do not commit, do not touch git.
Run tests with `python3 .g226/pt.py tests.guard.test_bounds ...` (dotted module names; prints per-class failure counts; full log in `.g226/last-<firstarg>.log`). Never run tools/run_tests.py (4 min, cargo).
Other people edit other test files in the same worktree at the same time: only run YOUR modules. `python3 -m ruff check --select F,E9 --no-cache <file>` must stay clean for your files.

## What D-049 changed (the whole point)
The operator VALUE limits are gone. The helper enforces only what the CPU / kernel enforces (read at runtime, fail closed when unread) plus PROCESS invariants.
REMOVED from `arltuner.guard.policy.Policy` (constructor / `dataclasses.replace` kwargs no longer exist; delete them from tests):
`assumed_bios_offset_mv`, `effective_offset_floor_mv`, `offset_ceiling_mv`, `max_ratio_step`, `max_v_step_mv`, `max_offset_step_mv`, `ratio_max` (+ "auto"), `v_target_max_mv` (+ "auto"),
`ratio_auto`, `v_target_auto`, `power` (PowerPolicy), `runtime` (RuntimePolicy). Also gone: `CheckContext.offset_floor_raw/offset_ceiling_raw/ratio_max/v_target_max_raw/max_*_step*/unlimited_steps`,
`policy.offset_floor_raw()`, `DEFAULT_*` constants, `PowerPolicy`, `RuntimePolicy`, `fused_ratio_cap`, `AUTO_OC_HEADROOM`, `Refusal`-side `STEP_LIMIT`/ramp rounds, `controls.caps.require_runtime_key`.
Read-only compat PROPERTIES remain on Policy with NEUTRAL values (for readers outside the package): `ratio_max`/`v_target_max_mv` = {P:None,E:None}, `offset_ceiling_mv` 999,
`effective_offset_floor_mv` -1000, `assumed_bios_offset_mv` {P:0,E:0}, `max_ratio_step` 255, `max_v_step_mv` 1000, `max_offset_step_mv` 2000, `power`/`runtime` None.
KEPT on Policy: `cpus, e_cpus, max_operations_per_session/boot, min_dwell_s, allow, msr_allow, source`, `hw_ratio_max` / `hw_v_target_max_mv` ({P,E} -> int|None: the CPU's own limits as
bound from tune.read via `with_hw_limits`; None = unread), and new `ignored_keys`.
`load_policy`: old keys (`effective_offset_floor_mv assumed_bios_offset_mv offset_ceiling_mv max_ratio_step max_v_step_mv max_offset_step_mv ratio_max v_target_max_mv [power] [runtime]`) still
LOAD, values are not even validated, they are ignored; `Policy.ignored_keys` names them (tables as `power.pl1_w` ...) and one WARNING is logged (logger `arltuner.policy`; file loads once per process).
Other unknown top-level keys are still a PolicyError.

### New judging rules (arltuner.guard.policy)
* `CheckContext.bound: bool` (set only by `build_context`) replaces the old "offset_floor_raw is None" no-context gate: `vf_bounded` / `vf_point_offset_bounded` refuse `ctx is None or not ctx.bound`.
  `CheckContext` has `hw_ratio_max` (CPU limit cmd 0x01 [7:0] of the kind, None=unread), `hw_v_max_raw` (cmd 0x28 [11:0]-1, raw S2.10, None=unread), `ratio_text`, `v_text` (source text; when unread it
  starts with the token `CPU_LIMIT_UNREAD` and names the read). `build_context(policy, cpu, *, favored_word, fused_word, vf_word_sent, max_voltage_word=None, oc_cap_words_of_kind=())`
  (no `favored_words_of_kind`). `ratio_limit(policy, kind, oc_cap_words=())`, `v_target_limit(policy, kind, max_voltage_word)` (raw), `v_target_limit_mv(...)` return `(value|None, text)`.
  `hw_v_max_raw(word)`: field [11:0]==0 means 2.0 V = raw 2048 (registers.md 0x28), else field-1. `hw_v_max_mv(None)` is None (unread), `hw_v_max_mv(0)` = 2000, `hw_v_max_mv(0x681)` = 1625.
* `vf_bounded` (0x11, P2=0), in this order: bound context; P2 0; adaptive only; ratio != 0; zero V target only for a no-OC-point word; references; then
  ratio: if the CPU ratio limit (0x01) is known, `new.ratio > limit` is refused ("above the CPU limit ... cmd 0x01"); if unknown, a RAISE (ratio above max(baseline ratio, 0x1C[7:0])) is refused
  with `CPU_LIMIT_UNREAD` in the detail; a non-raise passes. Voltage: predicted = (new V target, else the fused 0x09 voltage) + max(offset_raw,0); if the 0x28 limit is known predicted > limit is refused,
  if unknown a predicted voltage above the baseline's (max(base V target, fused V) + max(base offset,0)) is refused with `CPU_LIMIT_UNREAD`. Offset: only the 11-bit field and the allow entry's
  `params offset_raw_min/max`. NO offset window/ceiling/floor, NO step limits (a +/-500 mV offset or a 20-bin ratio step is accepted), NO v_target_max/ratio_max ceilings. Ordering rule
  (raise V first, lower ratio first; a word may not move both the same way) is unchanged.
* `vf_point_offset_bounded`: exact-match of ratio/V target/mode to the point's own baseline, offset field + params only. No window, no step.
* `turbo_ratio_bounded` (plain 0x650/0x1AD write): bytes non-zero; every byte <= CPU limit 0x01 of the kind when known; a byte above its baseline byte (raise) with the limit unread is refused
  (`CPU_LIMIT_UNREAD`). NO monotonic rule, NO step limit, NO base-ratio floor, NO policy ratio_max. `MsrCheckContext(kind, hw_ratio_max, ratio_text)`;
  `build_msr_context(policy, msr, *, oc_cap_words=None)`. `predict_msr(policy, req, baseline_word)` (no platform_info/favored_words).
* Guard (`WriteGuard`) behaviour otherwise unchanged (allowlists, baseline-first, read-back, ops counters, dwell, audit, health gate, write order).

### Planner (arltuner.controls.planner)
* No step limits: every plan is ONE round. `ramp_rounds(...)` always returns `(1, None)`; `ApplyPlan.next_profile_text` is always None; `NeedsRamp`/`NeedsRounds` never raised by the planner;
  `check_rounds` unchanged. The transition-safe write order (`controls.order`, voltage-first when raising) is unchanged, a ratio+V raise is still two 0x11 words.
* `oc_cap_kinds(policy, targets, table_kinds)`: the 0x01 read is planned for EVERY kind that has a ratio target (or turbo table) and whose `policy.hw_ratio_max[kind]` is None (it no longer depends
  on a policy ceiling) -> read counts of ratio plans on an unbound policy are +1 per kind. `needs_vmax(target)`: the 0x28 read is also planned for a positive whole-curve offset (not only an OC voltage).
* Warnings (strings in `ApplyPlan.warnings`, code-prefixed `CODE: text`; in `tune.plan` as `{code,text}` objects): `LARGE_CHANGE` (offset delta > 50 mV or ratio delta > 4), `POSITIVE_OFFSET`,
  `DEEP_WHOLE_CURVE` (whole-curve offset < -100 mV), `TURBO_NOT_MONOTONIC`, `TURBO_BELOW_BASE`, `TURBO_RAISE`. They never refuse. `plan_warnings()` no longer mentions a BIOS offset / floor.
  `preflight` no longer refuses by ceilings/windows: only field encoding (offset must fit -1000..+999 mV) and the allowlist/budget remain.
* `controls.power`: no `[power]` ranges/step: `plan(intent, policy, state, units, msr)` refuses only POWER_LOCKED / POWER_MSR_UNREADABLE / POWER_RANGE (PL<=0 or above 0x7FFF*unit) / POWER_VALUE /
  POWER_NO_FILE / NOTHING_TO_DO. PL2<PL1 and a >50 W change are warnings from `power.warnings(intent, state)` -> `[(code,text)]`; `power.limits_json(msr_view)`. `controls.caps`: no cap_ratio_min;
  CAPS_RANGE only above cpuinfo_max_freq (or below cpuinfo_min_freq when readable), CAPS_BELOW_MIN vs scaling_min_freq; `caps.warnings`, `caps.limits_json`. `controls.uncore`: only the hardware's
  initial_min/max range + min<=max (UNCORE_RANGE / UNCORE_ORDER); `uncore.warnings`, `uncore.limits_json`. `Policy.power`/`runtime` are None: tests must not need `[power]`/`[runtime]` to write.
* `helper.ops_tune`: `tune.read` now reads 0x01/0x28 for P, E AND the ring (P1=2): the limits group is 6 requests instead of 4 (session size `units + P + E-clusters + 4 + 6`); `hw_limits` stays
  {P,E}; new result key `cpu_limits` = {P:{max_mv,max_ratio,source}, E:{...}, ring:{...}, offset_mv:[-1000,999], power:{locked:None,pl_max_w:None,source}, caps_khz:None, uncore_khz:None}
  on tune.read and tune.last (survives a helper restart). `limits` (page contract) now has ratio_max/v_target_max_v = None, source "none", offset_mv window -1000..999. A turbo table that rises with
  the active-core count is a `TURBO_NOT_MONOTONIC` warning of tune.plan, no longer the `TURBO_ORDER` refusal. `STEP_LIMIT` is never produced. Plan-time range check (check code `POLICY_RANGE`) now
  fires only for: offset not fitting the field, V target / ratio above the CPU limit bound from tune.read. A guard refusal whose detail contains `CPU_LIMIT_UNREAD` becomes OpError check `CPU_LIMIT_UNREAD`.
* shipped `policies/default.toml` no longer has the removed keys.

## How to migrate a test
1. Keep the test's INTENT. If it only used a removed key as scaffolding (relaxing steps, setting a ceiling so a raise is possible, a stated BIOS offset), delete the kwarg / TOML key and fix expectations
   (read counts, step counts, 1 round instead of N).
2. If the test's ENTIRE subject is a removed rule (policy validation of removed keys, step-limit splitting/ramps/next-step profiles, ceiling/window/floor refusals, "shipped policy cannot overclock"),
   either INVERT it into the new behaviour when that is cheap and obvious (e.g. the +30 mV step that used to be refused is now accepted; the ceiling-refused raise is now allowed when the CPU limit is bound)
   or delete it. Keep a list of every deleted/inverted test with a one-line reason in your final report.
3. Do not weaken tests of PROCESS invariants (allowlist, one-transaction, read-back, audit, order, op counters, health gate, 0x1D/0x27/0x15 rules) - they must pass unchanged apart from setup.
4. Where the new CPU-limit rules need a limit the old test did not provide: bind the CPU limits in the test (`policy_with(...)` already binds `hw_ratio_max` P/E 120; V limit via
   `dataclasses.replace(policy, hw_v_target_max_mv={"P": 1625, "E": 1355})`, or a 0x28 word 0x681 / 0x56D in the session) rather than loosening the code.
5. A failing test you cannot explain from the rules above may be a SOURCE BUG: do not paper over it. Describe it precisely (test, observed vs expected, your hypothesis) in the report.
Final report (<= 250 words): files changed, tests run (counts before/after), deleted/inverted tests, suspected source bugs, requests for shared-support changes.
