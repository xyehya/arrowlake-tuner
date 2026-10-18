# ARCHITECTURE — contracts every module and worker must honour

> Carried over 2026-10-08 from the project's earlier research phase. Paths `docs/knowledge/*`, `docs/tasks/T-0xx` and `captures/*` cited here name notes of that phase, which are not part of this repository.

Language: **Python ≥ 3.12, standard library only** (`dataclasses`, `enum`, `typing.Protocol`, `tomllib`, `unittest`, `argparse`). No third-party dependencies (privileged code stays auditable). The mailbox layer is small and isolated so it can be ported later. Decision D-012.
Layout (src layout): `src/arltuner/<package>/`, tests in `tests/<package>/`, run all with `python tools/run_tests.py` (puts `src/` on the path, unittest discovery).
Facts live in `docs/knowledge/registers.md` — modules cite it; golden vectors in tests come from it.

```
arltuner/
  mailbox/    codec, pure, no I/O         (T-010)   words.py  vf.py  commands.py
  topology/   CPU map + XTU naming        (T-012)   cpumap.py
  profiles/   profile schema + loader     (T-012)   schema.py
  msr/        port contract, simulator, real /dev/cpu port, transport   (T-011)   port.py sim.py devcpu.py transport.py
  guard/      the ONLY write path         (T-013)   guard.py policy.py audit.py
  controls/   M1 control model + apply planner     (T-014)
  cli.py      plan / apply / read-vf front end     (T-014)
  health/     read-only snapshot/diff/gate          (T-015)   taint + PCI + modules; guard-compatible `health_gate(phase)`
Note: `msr.MailboxTimeout` has `.phase` ("before"/"after") and `.write_sent`; every mailbox request — reads too — needs a *writable* port (it is a WRMSR).
```
Dependency direction: `cli → controls → guard → msr.transport → mailbox`; `topology` and `profiles` are leaves used by `controls`. No module imports "upward". Only `msr/devcpu.py` touches the real device, and only `guard` may call `MailboxTransport.transact` for any command (every mailbox command is a WRMSR).

## Contract 1 — `arltuner.mailbox.words` (T-010)
```python
MSR_OC_MAILBOX = 0x150
def pack(cmd: int, p1: int, p2: int, data: int) -> int:      # (1<<63)|(p2<<48)|(p1<<40)|(cmd<<32)|data ; ValueError if a field is out of range (cmd,p1,p2: 0..255; data: 0..2**32-1)
def is_busy(raw: int) -> bool:                               # bit 63
@dataclass(frozen=True) class Response: rc: int; data: int   # rc = (raw>>32)&0xFF ; data = raw & 0xFFFFFFFF
def unpack(raw: int) -> Response
```
## Contract 2 — `arltuner.mailbox.vf` (T-010)
```python
@dataclass(frozen=True) class VfWord:
    ratio: int            # [7:0]   0..255
    v_target_raw: int     # [19:8]  0..4095, volts = raw/1024
    static: bool          # [20]    False = adaptive (M1 never sets True)
    offset_raw: int       # [31:21] signed 11-bit, -1024..1023, volts = raw/1024
    @classmethod
    def from_u32(cls, w: int) -> "VfWord"
    def to_u32(self) -> int
    @property v_target_v -> float ; @property offset_mv -> float      # offset_raw*1000/1024
    @staticmethod v_to_raw(volts: float) -> int                      # round(volts*1024)
    @staticmethod mv_to_offset_raw(mv: float) -> int                 # XTU/ThrottleStop rounding: (|mv|*1024+512)//1000, sign preserved; ValueError outside -1024..1023
    def with_(self, **changes) -> "VfWord"                           # dataclasses.replace with validation
```
`arltuner.mailbox.commands`: `IntEnum`s `Cmd` (every command in registers.md, read+write ids), `Domain` (P1 wire values: CORE 0, GT 1, RING 2, GT_UNSLICE 3, SA 4, MEMSS 6, NGU 7), `Rc` (all completion codes; `rc_name(code)` returns `UNKNOWN_0x..` for undefined such as 0x11), `WRITE_COMMANDS` (frozenset of ids that change state), helper `is_write(cmd)`.

## Contract 3 — `arltuner.topology` (T-012)
```python
@dataclass(frozen=True) class Core: kind: str        # "P" | "E"
                                    index: int       # XTU numbering: P 0..7, E 0..15
                                    cpu: int         # Linux CPU number (P 0..7, E 8..23)
                                    cluster: int | None   # E: 0..3 (cpus 8-11,12-15,16-19,20-23); P: None
                                    favored: bool
class Topology:
    cores: tuple[Core, ...]
    def by_cpu(self, cpu: int) -> Core ; def resolve(self, kind: str, index: int) -> Core
    def cluster_cpus(self, cluster: int) -> tuple[int, ...] ; def xtu_name(self, core: Core) -> str   # "P-Core 4" / "E-Core 12"
def arl_s_285k() -> Topology                          # from platforms/arl-s.toml (G-101/G-128, arltuner.platform)
def detect(sysfs_root: Path = Path("/sys")) -> Topology  # /sys/devices/cpu_core/cpus + /sys/devices/cpu_atom/cpus ; falls back/validates against arl_s_285k(); raises TopologyMismatch
```
## Contract 4 — `arltuner.msr` (T-011)
```python
class MsrError(OSError): ...
class MsrPort(Protocol):
    writable: bool
    def rdmsr(self, cpu: int, msr: int) -> int            # raises MsrError
    def wrmsr(self, cpu: int, msr: int, value: int) -> None   # raises MsrError; PermissionError-like if not writable
class MailboxTimeout(Exception): ...
class MailboxTransport:
    def __init__(self, port: MsrPort, *, poll_sleep_s: float = 0.01, timeout_s: float = 10.0, sleep=time.sleep, clock=time.monotonic)
    def transact(self, cpu: int, cmd: int, p1: int, p2: int, data: int = 0) -> Response
        # 1) rdmsr 0x150 until bit63==0 (timeout -> MailboxTimeout, NOTHING written) 2) wrmsr pack(...) 3) poll until bit63==0 (timeout -> MailboxTimeout) 4) return unpack(raw)
class SimMsrPort(MsrPort): ...    # models the pcode mailbox per CPU: busy bit, cmd 0x10/0x11 V/F word state per (cpu,p1,p2), 0x14/0x15 per-core enable, 0x01/0x02/0x28 capabilities, RC rules (OCLocked, RatioExceedsLimits, UnderVoltProtection, UnsupportedDomain...), fault injection (stuck busy, rc errors, EIO)
class DevCpuMsrPort(MsrPort): ... # /dev/cpu/N/msr; O_RDONLY unless constructed writable=True (then O_RDWR); Linux only; never used by tests except against fake files
```
## Contract 5 — `arltuner.guard` (T-013) — **the only path that may call `MailboxTransport.transact`**
```python
@dataclass(frozen=True) class Request: cpu: int; cmd: int; p1: int; p2: int; data: int; purpose: str
@dataclass(frozen=True) class Confirmation: token: str; operator: str      # human-typed per session; env ARLTUNER_ALLOW_MAILBOX=1 also required
class Policy:                      # loaded from a reviewed file; allowlist of (cmd, p1, p2-range, data mask/bounds, cpu set); bounds (D-016): per-kind ratio ceiling, adaptive V-target ceiling, offset floor/ceiling counting the BIOS offset (D-015), per-operation step limits, mode adaptive (static/Power-Gate refused) — **the code still enforces the old undervolt-only predicate until T-022**, min dwell between transactions, max transactions per session (default 1 until raised by the human)
class WriteGuard:
    def __init__(self, transport, policy, audit, *, health_gate=None, lock_path=..., clock=time.monotonic)
    def execute(self, req: Request, confirm: Confirmation) -> Result      # **T-013 is authoritative** (docs/tasks/T-013-guard.md): ordered fail-closed checks (tripped, env flag, confirmation, cpu, allowlist, baseline-read-first, predicate, session limit, dwell, lock file, health gate); transact; read-back verify where a read twin exists; audit everything
    def revert_plan(self) -> list[Request]                                # inverse requests built from the baseline words read earlier in the session (never executed by the guard)
```
Refusals raise `GuardRefused(reason)`; nothing is sent. `AuditLog` appends JSON lines (never overwrites).
## Contract 6 — `arltuner.profiles` (T-012)
TOML profile → validated `Profile`: `[meta] name, platform="arl-s-285k"`, `[[core]] kind="P"|"E", index, ratio?, oc_voltage_mv?, offset_mv?`. E-core entries in one cluster must agree (hardware regulator is per cluster; `[UNVERIFIED]` whether a write is cluster-wide). Range checks only (types, units); **safety bounds belong to `guard.Policy`, not the profile**.

## Testing rules
Golden vectors come from `registers.md` (listed in each task). Simulator-only; no test opens a real `/dev/cpu/*/msr`. Every refusal path has a test that must fail if the refusal is removed. `python tools/run_tests.py` must stay green on Windows and Fedora (no OS-specific imports at module import time).

## Update 2026-10-01 — T-021 merged (XTU-measured alignment; contracts 4–7 changed)
- **`msr/measured_baseline.py`** (new, data only, `[MEASURED win 2026-10-01]`, Linux CPU numbering via the LP map) + **`SimConfig.measured(reject_zero_fields=True)`**; the CLI's `--sim` now defaults to it. `SimConfig` gains `reject_zero_fields`, `fixed_reads`; the sim models `0x09 P1=0x11` per CPU, `0x1C`/`0x1D`, `0x26`/`0x27`, per-CPU `0x28` (accessors `favored_word`/`fused_word`/`granular_word`).
- **Planner step list:** `0x14` once; per CPU `0x10, 0x1C, 0x09 P1=0x11, 0x26`; then `0x15` (only if bit 3 clear, placed after all reads), then per CPU `0x11` (ratio = non-zero baseline ratio else `0x1C[7:0]`; V target = fused voltage; adaptive; only the offset changed), `0x1D` (= ratio), `0x27` (= the `0x26` value): **2 + 7n operations**, pre-flighted (`NeedsHigherLimit`, `NeedsBootBudget`). `ApplyReport.zero_baseline_cpus`, new `StepKind`s, `CoreReadings`; revert plan is XTU-shaped (offset restored, never a zero field; "baseline 0x0 cannot be restored by a write; reboot restores it").
- **Guard:** checks now take a `CheckContext` (`CheckContext`/`build_context` live in `guard.policy`); new reads `0x1C`, `0x26`, `0x28`, `0x09 P1=0x11`; writes `0x1D` (data must equal the ratio of the `0x11` executed for that CPU this session — no ceiling) and `0x27` (data must equal the `0x26` value read); read twins `0x1D→0x1C`, `0x27→0x26` with their own read-back predicates (a mismatch trips the guard; **`0x1C` returning the written low byte is `[UNVERIFIED]`**); `vf_undervolt` refuses a zero ratio or V target and uses the session's `0x1C`/`0x09` for its ceilings. `Policy` gains D-015 fields `assumed_bios_offset_mv`, `effective_offset_floor_mv`, `e_cpus` (TOML keys too; defaults P −50 mV, E −100 mV runtime floor).
- The ratio ceiling is `0x1C[7:0]` (or the non-zero baseline ratio if smaller); **`0x1C[15:8]` is not a ratio cap on E cores** (it reads 46 while XTU wrote 48).
- **Superseded by D-016 / T-022:** the "undervolt-only / offset ≤ 0" predicates described above are the current default and are replaced by bounds in T-022.

## Update 2026-10-01 — T-022 merged (bounds, not direction; D-016)
- **Guard:** `vf_bounded` replaces `vf_undervolt`: adaptive only; non-zero ratio and V target; ratio <= `ratio_max[kind]`, V target <= `v_target_max_mv[kind]` and <= the session's `0x28` hardware maximum, offset inside the BIOS-aware window (D-015) and <= `offset_ceiling_mv`; per-operation step limits; voltage-first / frequency-first ordering; one `0x11` may not raise ratio and V target together. **Defaults are the no-raise ceilings, so the shipped policy refuses every raise**; numbers come from a reviewed TOML (`policies/example-oc-ramp.toml` is an inert example; strict schema, sanity bounds).
- **Planner/controls:** `NeedsRamp(next profile text)` when a request exceeds the step limits; two `0x11` words for a raise; `0x28` read when the V target rises; revert plan undoes writes newest-first through the state each left (frequency first for a raise, never a zero field); budget counts read-backs.

## Update — T-023 (guarded plain-MSR path + turbo ratio tables; D-017; contracts 4, 5, 6 and the controls changed)
- **Guard (contract 5):** `WriteGuard(..., msr_port=None)` (structural `MsrPortLike`: `rdmsr`/`wrmsr`; without it every plain request is refused `NOT_ALLOWLISTED`). `execute()` takes a mailbox `Request` **or** `MsrRequest(cpu, msr, value, purpose, kind="read"|"write", verify_cpu)` and returns `Result` / `MsrResult(value, verify_value, verified)`. Same ordered chain (TRIPPED, env flag, confirmation, CPU, allowlist = `Policy.msr_allow`, baseline = an `rdmsr` of the same MSR on the same CPU this session, check, session cap, boot cap, dwell, lock, health gate, audit, read-back, trip). Plain **reads** are audited (new event `msr_read`, never `attempt`), pass the same limit checks but do not count toward the session / per-boot caps and do not start a dwell; plain **writes** count like a WRMSR. Write read-back: `rdmsr` on the same CPU, then on `verify_cpu` (must differ from the CPU, be in the policy and be of the *other core kind*, with a read entry covering both); either mismatch trips. `revert_plan()` may contain `MsrRequest`s (the baseline word; an undo that lowers a bucket comes before the mailbox undos, one that raises a bucket after them).
- **Policy:** `[[msr_allow]]` (`name, msr, kind, cpus?, check?`) -> `MsrAllowEntry`, `Policy.msr_allow` (default `()` = every plain access denied), `Policy.match_msr`. Readable: `0x1AD 0x1AE 0x650 0x651 0xCE 0x194`; writable only `0x650` (E) / `0x1AD` (P) with check `turbo_ratio_bounded` (new registry `MSR_CHECKS`, kept apart from `CHECKS`); `0x651`/`0x1AE` writes are a load error. **`default_m1_policy()` allows the six plain reads and no plain write.** `turbo_ratio_bounded`: baseline word read this session; every byte non-zero; non-increasing bucket 0..7; each byte <= `ratio_max[kind]` (kind from the MSR, not the CPU; no ceiling = <= its baseline byte); |byte - baseline byte| <= `max_ratio_step`; every byte >= `0xCE[15:8]` if `0xCE` was read, else >= baseline minimum - step. Audit records of plain requests carry `msr`, `msr_kind`, a 16-digit `req_data`/`value`.
- **Simulator:** plain registers `0x1AD 0x1AE 0x650 0x651 0xCE 0x194` (`[MEASURED lnx 2026-10-01]` values) in every `SimConfig`, package-scoped; `plain_msrs_per_cpu=True` makes them per CPU; `plain_msrs=False` removes them; `plain_msr_values` overrides; `SimMsrPort.plain_msr()` peeks, `ignore_plain_writes(msr)` is a fault; `0x1AE/0x651/0xCE/0x194` are read-only (EIO).
- **Profile (contract 6):** optional `[turbo]` with `e = [8 ints]` (-> `0x650`) and/or `p = [8 ints]` (-> `0x1AD`), bucket 0 first, each 1..255 (`Profile.turbo: TurboTables | None`). A profile still needs at least one `[[core]]`.
- **Planner/executor/report:** plain `rdmsr` steps (`StepKind.READ_MSR`: `0xCE` and the tables; or `0x650` alone, if the policy allows it, when an E ratio is set without a `[turbo] e` table) come **before** the mailbox reads, on the first P CPU; `StepKind.WRITE_MSR` per table: a table that raises any bucket is written **last**, one that only lowers goes **first** (after the `0x15`); a table that does both is a refused step (split it). The budget (`required_operations`, `count_operations(..., msr_writes)`, `preflight`) counts the plain writes only (plain reads and the `rdmsr` read-backs are not WRMSRs); `ApplyReport.operations` counts mailbox operations only (`plain_msr_operations` the rest). A raise above the current `0x650` maximum with no E table is warned about (never an invented table). `plan` shows the buckets before/after; `PlanStep.guard_request()`, `PredictedRefusal.msr`, `StepResult.verify_readback_data`, `predict_msr`, `pack_turbo`, `turbo_cpus` are new. CLI: the guard gets `msr_port=port`; `--sim` plans peek `SimMsrPort.plain_msr`.
- **Changed existing contracts/tests:** the example policy's E `ratio_max` is 49 (was 50; D-017) so `tests/test_cli.py` `RaiseThroughTheCli` moved its E-ceiling/ramp cases by one; `tests/controls/_support.make_session` passes `msr_port`. `[UNVERIFIED]`: that the hardware accepts a raised table and the scope (package/core) of the `0x650`/`0x1AD` write.

## Update — T-025 (per-V/F-point offsets; contracts 4, 5, 6 and the controls changed)
- **Guard (contract 5):** new check `vf_point_offset_bounded` (registry `CHECKS`, params `offset_raw_min` / `offset_raw_max` like `vf_bounded`) for `0x11` with `P1 = 0` and `P2` 1..15. Baseline = the `0x10` read of the **same CPU and same P2** this session (the guard's baselines were already keyed cpu/p1/p2); the word must equal the baseline in ratio `[7:0]`, V target `[19:8]` and mode bit `[20]` (only `[31:21]` may change; a baseline with ratio 0 or the static bit is refused); offset inside the whole-curve window (`effective_offset_floor_mv` - `assumed_bios_offset_mv` .. `offset_ceiling_mv`) and `|new - baseline| <= max_offset_step_mv`. Read-back = whole word, as for `P2 = 0`. **Revert = the baseline point word exactly** (never filled from `0x09`); the `0x1D` revert and the `0x11` facts (`vf_word_sent`, `0x1C`, `0x09`, `0x28`) concern `P2 = 0` only. **Binding (fail closed):** `AllowEntry` refuses `vf_point_offset_bounded` unless cmd `0x11`, `p1 = [0]`, `p2 ⊆ 1..15` (`POINT_P2_MAX`), and `vf_bounded` unless `p2 = [0]`; each check also refuses the other's addresses (tightening of `vf_bounded`: `P2 != 0` is refused). **`default_m1_policy()` is unchanged: no point read or write entry (inert).** Monotonicity V(n) >= V(n-1) is the hardware's, not checked.
- **Simulator (contract 4):** `SimConfig.point_ratios` ((kind, point) -> ratio field of an unwritten point; default `{("E", 6): 46}` = the measured word `0x2E`), `point_counts` (kind -> fused points; **default `None` = the legacy 1..15**; `SimConfig.measured()` sets E 6 / P 8; `P2` above it -> rc `0x11` = `sim.RC_NO_SUCH_POINT`); `reject_zero_fields` applies to `P2 = 0` only (a point word's V target is 0); `--sim` of the CLI now runs with `per_point=True`.
- **Profile (contract 6):** `[[point]]` (`kind`, `index`, `point` E 1..6 / P 1..8, `offset_mv`, optional `expect_ratio`) -> `Profile.points: tuple[PointSetting, ...]`, `Profile.expand_points`; E entries are cluster-wide (same (cluster, point) must agree); no duplicate (kind, index, point). A profile needs at least one `[[core]]` **or** `[[point]]` (a `[turbo]`-only profile is still empty; message now names both).
- **Planner/executor/report:** `StepKind.READ_VF_POINT` / `WRITE_VF_POINT`, `PointTarget`, `ApplyPlan.point_targets`, `resolve_point_targets`, `build_plan(..., point_words={(cpu, point): word})`, `count_operations(..., n_points)` (3 WRMSRs per point CPU), `plan_warnings(..., point_targets)` (+ `POINT_CAVEAT`; the XTU-shape caveat is dropped for a point-only profile). Reads: `0x14`, then each `0x10 P2=point`, then the core reads; writes: `0x15` rule as for cores, a lowering-only turbo table, **all point writes, then all `[[core]]` words**, a raising turbo table. No `0x1D` / `0x27` for a point. `expect_ratio` mismatch = predicted `CHECK_FAILED` on the write step; a step beyond `max_offset_step_mv` = `NeedsRamp` with `[[point]]` entries in the next profile. `format_preview(..., points)`; `resolve_targets` may return `()` for a point-only profile.
- **Changed existing tests:** `tests/guard/test_policy.py` `test_check_registry_is_fixed` lists the new check (registry grew); no other existing test was altered. `[UNVERIFIED]`: that the hardware accepts a per-point write on Linux and what its read-back returns.

## Update — T-026 (`arltuner vf-dump`: the V/F curve of every core; contracts 4 and the CLI changed)
- **CLI:** `vf-dump [--sim|--device] --policy FILE [--cpus LIST] [--all-e] [--measure] [--json FILE] [--audit FILE] [--lock FILE] [--allow-taint-bit N]`. One guarded session, one `CONFIRM`; per CPU `0x10 P1=0 P2=0` (OC word) then `P2 = 1, 2, ...` until the first rc `0x11` (expected end of a table). **The known count (P 8, E 6) is not a stop**: tables are per core (57x is the boost of the favored P-cores 0 and 3 only) and an OC ratio above the top fused ratio adds one point (`[COMMUNITY]`, SkatterBencher; operator 2026-10-02). Hard cap `P2 <= 10` (P) / `<= 8` (E): reaching it without an rc `0x11` is reported (`cap reached ..., no rc 0x11 seen`), not an error. Default set: 8 P-cores + the lowest CPU of each E-cluster (8, 12, 16, 20): **budget 124** (8 x 11 + 4 x 9), normally **112** reads; `--all-e` (16 E CPUs): budget **232**, normally **208**; `--cpus` overrides both. Any other rc (also `0x11` at `P2 = 0`), a timeout, a guard refusal / trip or Ctrl-C stops the whole dump: what was read is printed (and written to `--json`, `complete: false`), exit 3 (a non-trip guard refusal: 2). **Every request is checked before anything is sent (worst case = the caps):** an allow entry of `kind = "read"` for each `(cpu, 0x10, 0, P2)` up to the cap (a policy must allow `P2` 1..10), the CPU set, the session cap (`needed`; the refusal names the count) and the per-boot cap minus the audit log's count. The guard gets `max_operations_per_session = needed` (+1 with `--measure`). Dwell: the command waits `min_dwell_s` (+0.05 s) between reads on the guard's own clock/sleep (the guard refuses, never sleeps).
- **`--measure`** (per P-core and per E-cluster, every point ratio and the OC ratio if above the top point): cap cpufreq `scaling_max_freq` (P `ratio x 100 MHz`, E `(ratio + 1) x 100 MHz`, `[MEASURED lnx 2026-10-02]`), load all CPUs of the group (`stress-ng --cpu n --taskset`), 5 samples of MSR `0x198` on the first CPU read (`[47:32]/8192` V, `[15:8]` must equal the target, else `MISMATCH` and no voltage). `0x198` is read **through the guard** (`MsrRequest`, `msr_allow`); the caps are restored in a `finally` (also on Ctrl-C; a failed restore is a loud warning + exit 3). **Every VID is recorded with the core temperature at sampling time** (TVB moves the voltage ~1.1 mV per degree C, SkatterBencher `[COMMUNITY]`): `MeasureIO.read_temp_c(cpu)` = coretemp hwmon (`hwmon*/name == coretemp`, `temp*_label == "Core <core_id>"`, core_id from `cpuN/topology/core_id`), sysfs only because MSR `0x19C` / `0x1A2` are not in `PLAIN_MSR_READABLE`; shown as `VID V @ 63 °C` (a range if it moved), `@ n/a` when there is no sensor (E-cores may have no `Core N` label), JSON `temp_min_c` / `temp_max_c`. A ratio whose own frequency exceeds the current `scaling_max_freq` is skipped with a note. `--measure` needs `--device`; in `--sim` it is refused unless a test injects `measure_io_factory`. **Budget: `needed + 1`** in both caps, because the guard refuses even a plain read (`SESSION_LIMIT` / `BOOT_LIMIT`) once a cap is full (a plain read does not count, but is not exempt from the check). **Blocker: `0x198` is not in `PLAIN_MSR_READABLE` (`guard/policy.py`), so no loadable policy can allow it yet; `--measure` refuses (exit 2, nothing sent) until the orchestrator adds it.**
- **Module:** `src/arltuner/vfdump.py` (not `controls/`: the static rules forbid `subprocess` there; `SysfsMeasureIO` is the only place that spawns `stress-ng`, built only for `--device`). Output: a table per P-core / E-cluster (point, ratio, offset mV + raw, measured VID), the decoded OC word, a note when a table has more points than the known count, and flagged differences (offset **or point ratio** between P-cores at the same point, values grouped by CPU; between the CPUs of one E-cluster that were read; the OC ratio is not compared). `--json` carries the same data (raw words kept).
- **Simulator (contract 4):** `SimConfig.point_offsets` ((kind, point) -> raw offset), `SimConfig.cpu_point_offsets` ((cpu, point) -> raw; wins), `POINT_RATIOS_ARL_S` / `POINT_OFFSETS_ARL_S` (the measured P and E tables with the BIOS offsets); **`SimConfig.measured()` now uses them** (`SimConfig()` keeps its old defaults); `cpu_point_ratios` ((cpu, point) -> ratio, per-core tables; `measured()` gives the six non-favored P-cores the SIM-ONLY table `POINT_RATIOS_P_NONFAVORED_SIM` 8,20,30,45,50,52,54,55 unless the caller passes its own `point_ratios`); `oc_adds_point` (default False): an OC word ratio above the top fused ratio of the CPU makes `P2 = count + 1` answer with that ratio, rc `0x11` after it. No existing test changed.

## Update — T-027 (`vf-dump --measure`: robust sampling and adaptive caps)
- **Sampling:** 30 samples at 0.1 s per cap attempt (`SAMPLES`, `SAMPLE_INTERVAL_S`; `measure_group(samples=, interval_s=, min_valid=)`, no CLI flag yet). Only samples whose running ratio (`0x198[15:8]`) equals the target are kept; the VID range is over those. `ok` if at least `min_valid` (default 5) were kept, else `MISMATCH`. Text shows `[kept/total at target]` when some were dropped; JSON has `samples_kept` / `samples_total`.
- **Cap search** (replaces the fixed P `ratio` / E `ratio + 1` rule, which was ratio-dependent for E, `docs/knowledge/power-profile-capping.md` §6): try `ratio x 100 MHz`; if the running ratio settles below the target, retry `(ratio + 1) x 100 MHz`; above it, or still short after the retry, is `MISMATCH`. A cap above `cpuinfo_max_freq` (new `MeasureIO.read_max_khz`) is never offered. `cap_khz` in the result is the cap of the reported attempt; a `+1` cap is noted. Caps are still restored in the `finally`. Guard budget is unchanged (plain reads do not count against the attempt cap); a retry doubles the audited `0x198` reads for that ratio.

## Update — T-028

`vf_point_offset_bounded` now accepts `P1 in {0, 2}` (core, ring) with `P2` 1..15; an allow entry may bind `p1` only to a subset of `{0, 2}` (P1 1 and 7 stay refused). `arltuner point-offset --cpu N [--p1 0|2] --p2 N --offset-mv X [--expect-ratio R] [--sim|--device]` runs one guarded session of two operations: read `0x10 P1/P2` (baseline), then write `0x11` with only `[31:21]` replaced (the guard reads it back), and prints before / after / the revert word. The simulator models the ring point table (8,14,20,28,34,37,38,39x; rc `0x11` past point 8; point 8 starts at offset raw -77). The offset window uses the CPU's kind: with the shipped `assumed_bios_offset_mv` P = -50 the floor is -51 raw, so the ring's BIOS -77 needs a policy with `assumed_bios_offset_mv = { P = 0 }`.

## Contract 8 — helper ↔ server protocol (SPEC §4.5; G1, pinned before G-102/G-103)
- **Transport:** UTF-8 JSON, one object per line (`\n`), over the helper's stdin (requests) / stdout (responses, events); stderr is log only.
  Max line 64 KiB in both directions; an oversize or unparsable line ⇒ `{"id": null, "ok": false, "error": {"code": "BAD_REQUEST", ...}}`, helper keeps running.
- **Request:** `{"id": <str, ≤ 64 chars, unique per connection>, "op": <str>, "args": <object>}` — no other top-level keys (unknown ⇒ `BAD_REQUEST`).
- **Response (exactly one per request):** `{"id", "ok": true, "result": <object>}` or `{"id", "ok": false, "error": {"code": <UPPER_SNAKE>, "message": <str>,
  "detail": <object, optional>}}`. Codes used so far: `BAD_REQUEST`, `UNKNOWN_OP`, `BUSY` (state machine conflict; `detail.state`), `REFUSED` (guard
  refusal; `detail.check`), `PREFLIGHT_FAILED`, `PLAN_STALE`, `CONFIRM_MISMATCH`, `CANCELLED`, `INTERNAL`.
- **Event:** `{"event": <str>, "op_id": <request id or null>, ...}` — never carries `"id"`. Names: `heartbeat` (every 2 s, `{"state"}`), `progress`
  (`{"step", "of", "ops_used", "ops_budget"}`), `telemetry` (`{"t", "values"}`), `state` (`{"state"}`).
- **Plan/apply (SPEC §4.3):** `plan` → `result: {"plan_id": <sha256 hex>, "nonce": <6 chars [A-Z2-9]>, "steps": [...], "ops_needed", "ops_left",
  "expires_s"}`; `apply` args `{"plan_id", "typed"}` — accepted only when `typed == "CONFIRM " + nonce`, plan unchanged, < 600 s old, preflight passes;
  a nonce is single-use (replay ⇒ `CONFIRM_MISMATCH`).
- Ops: SPEC §4.5 list; an op not yet implemented ⇒ `UNKNOWN_OP`. Tests use a fake helper that speaks this contract exactly; changes need an update here.
- **Addendum (D-027, G1):** (a) **Long ops** (`apply`, `restore`, `vf.dump`, `stress.*`): the helper still sends exactly one response, at the end;
  the server answers the POST at once with `202 {"id"}` and forwards the helper's final response as an SSE event `result` (`{"id", "ok", ...}`).
  No HTTP timeout ever reports a still-running op as failed. (b) **SSE auth:** cookie + exact Host + `Sec-Fetch-Site: same-origin` (absent ⇒ refuse);
  `Origin`, when sent, must be exact — browsers omit it on same-origin GET, so it cannot be required there. (c) **Preflight `UNKNOWN`/unprivileged:**
  the server's preflight is display-only; the root helper re-runs preflight and refuses any write op unless the mode is `write` with no check
  `UNKNOWN`. (d) **`/api/call`** forwards only an allowlisted `op` set with schema-checked `args`; never a raw passthrough.
- **Addendum (G-102 merged, D-029):** `args` optional (default `{}`). Op args/results: `preflight {}` → report + `writes_allowed`; `vf.dump
  {cpus?: [int] | "0-7,16", all_e?: bool}` (long; state `planning`; **write-gated**, a mailbox read is a WRMSR) → `vfdump.to_json` + `ops_done`,
  partial in `detail.partial` on cancel/refusal; `audit.tail {n?: 1..200}` (last 256 KiB only); `telemetry.start {interval_s?: 0.2..60}`;
  `cancel {op_id?}`. Duplicate in-flight request (same id/op/args) is deduped; other id reuse ⇒ `BAD_REQUEST` (id null); seen-id LRU 1024; input
  queue 64 ⇒ `BUSY detail.queue="full"`. `detail.check` values: `TRUST_PATH`, `LOCK_UNTRUSTED`, `INSTANCE_LOCK`, `TOPOLOGY`, `UNKNOWN_CORE`,
  `POLICY_INVALID`, `SESSION_LIMIT`, `BOOT_LIMIT`, `AUDIT_UNAVAILABLE`, `NO_HEALTH_GATE`, `STARTUP`, `HELPER_CONFIG_INVALID`; hardware faults and
  guard trips ⇒ `INTERNAL`. As root the helper takes **no argv but a bare `--device`** (`--sim` ⇒ `ROOT_SIM`, others ⇒ `ROOT_ARGV`, exit 77); paths are
  constants (`/etc/arltuner/policy.toml`, `/etc/arltuner/helper.toml` = `allow_taint_bits`, `/var/lib/arltuner/audit.jsonl`, `/etc/arltuner/accepted`);
  every root-opened file: `O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC`, fstat regular, nlink 1, owner euid, no g/o write, never `O_TRUNC`. Server `OPS` mirrors these.
- **Addendum (G-107, D-030):** `heartbeat {state, ops_used, ops_budget}` (per-boot count from the helper's audit log; budget from root policy) —
  the dashboard's budget source. `vf.dump` result gains `accepted_match`: per core `{cpu, match: bool, diffs: [{point, expected, actual}]}` computed by
  the helper against `/etc/arltuner/accepted` EXPECTED words; `null` when no trusted accepted state exists.

## Contract 9 — v3 ops (SPEC-v3, D-032/D-033; pinned 2026-10-11 before G-111/G-112/G-113)
Same transport/response/event rules as contract 8. `R` = telemetry reading `{value|null, unit, source, status: "ok"|"unavailable", reason?}`.
Units: `"P0".."P7"` (cpus `[n]`), `"EC0".."EC3"` (cpus `[8..11]`, `[12..15]`, `[16..19]`, `[20..23]`). Long ops answer at the end (202 + SSE
`result`, D-027) and emit `progress`. Server `OPS` allowlists every op below with the same arg schema; page `ALLOWED_OPS` gains them.
- **`telemetry.start {interval_s?: 0.2..60}`** (changed): if running with a different interval ⇒ stop + restart at the new one, result
  `{running: true, restarted: bool, interval_s}`; same interval ⇒ `already_running: true` as before.
- **`mem.read {}`** (short; root; not write-gated: no mailbox, no write) ⇒ `{available: bool, reason?, mts, qclk_mhz, gear, cmd_rate: "1T"|"2T",
  bclk_mhz, timings: {tCL, tRCD, tRP, tRAS, tRC, tRFC, tRFCpb, tREFI, tFAW, tRRD_L, tRRD_S, tCWL, tRTP, tWR, tWTP, tRCDW, tRPab, tREFSBRD},
  turnaround: {RDRD|RDWR|WRRD|WRWR: {SG, DG, SD, DD}}, instances_ok: int, cross_check: {agree: bool|null, mhz_a, mhz_b}, max_bandwidth_mbs,
  dimms: [{locator, size, part, manufacturer, speed, configured_speed, voltage, rank, temp_c: R}]}` (knowledge `memory-mchbar.md`).
- **`vf.measure {mode: "quick"|"dense", units?: [unit] (default all 12), name?: str ≤ 64}`** (long; state `measuring`; **not mailbox**; writes
  cpufreq only, journaled) ⇒ `{curve_id, record}`. `progress` events: `{step, of, unit, ratio, eta_s}`; per finished point an event
  `vf.point {unit, ratio, mhz, vid_v, status}` so the chart fills live. Cancel ⇒ `CANCELLED` with `detail.partial` = the record so far (also stored,
  `complete: false`). Caps (min and max) restored in every exit path and from a stale journal at helper start (EPP is no longer touched, G-132).
- **Curve record** `{id, name, created, boot_id, mode, complete, kernel, tuning_state: {kind: "stock"|"words"|"unknown", words?: {cpu: [hex]}},
  temps: {min_c, max_c}, units: {unit: {cpus: [int], points: [{ratio, mhz, vid_v, vid_min, vid_max, kept, total, status:
  "ok"|"mismatch"|"unreachable"}]}}, baseline: bool}`.
- **`curves.list {}`** ⇒ `{curves: [{id, name, created, mode, complete, tuning_state.kind, units: [unit], baseline}]}` (newest first);
  **`curves.get {id}`** ⇒ record; **`curves.update {id, name?, baseline?: bool}`** (one baseline at most); **`curves.delete {id}`**.
  Storage `/var/lib/arltuner/curves/` root 0700, files 0600, id `^[0-9]{8}T[0-9]{6}Z-[a-z0-9]{4}$`, same open rules as contract 8.
- **`tune.read {}`** = old `vf.dump` (alias kept), result adds per unit/point decoded `{ratio, offset_mv, v_target_v|null, mode}`. G-212: result (and `tune.last`'s `result`) carries `intent_features: [str]` — the optional `tune.plan` intent keys this helper understands (`["ring"]`); a front-end gates its ring editor on it.
- **`tune.plan {intent}`** (short) — `intent`: `{scope: "simple"|"unit", rows: [{target: "P"|"E"|unit, offset: {kind: "curve", mv} | {kind:
  "points", mv: {ratio: mv}} | null, v_target_v: float|null, ratio: int|null}], turbo?: {P?, E?: [8 ratios]}, ring?: {points: {"<P2>": mv}}}`
  (`rows` + `scope` together; `turbo` and `ring` may stand alone or beside them; **G-212 `ring`**: the ring (cache, P1 = 2) points keyed by the **P2 point
  number** 1..15 of the last ring read (not by ratio), offset-only; the ring OC word (P2 = 0) is BIOS-owned, taken from the last read and never written: a
  live OC word that differs refuses the ring unit alone (`ring_refused`, warning `RING_NOT_RESTORED`); each point must sit in the policy `0x11 P1=2` entry's
  window else `REFUSED POLICY_RANGE`; needs a ring read this boot (`NO_READ`); not savable via `profiles.save`) or `{profile: name}` or `{reset: "stock"}` or
  `{restore: "accepted"}` ⇒ contract 8 plan result + `before`/`after` per unit/point `{ratio, offset_mv, v_target_v}`, `warnings: [str]`.
  Out-of-policy ⇒ `REFUSED detail.check="POLICY_RANGE"` naming the field and the limit.
- **`tune.apply {plan_id, typed}`** (long; write-gated) — contract 8 apply rules ⇒ `{steps: [...], readback: [{unit, point, expected, actual,
  match}], ops_used}`.
- **`profiles.list {}`** ⇒ `{profiles: [{name, source: "accepted"|"saved", created, complete}]}`; **`profiles.save {name, intent}`** (never overwrite ⇒
  `REFUSED detail.check="EXISTS"`); stored `/var/lib/arltuner/profiles/` (root, same rules). G-148 (D-044): see the addendum "G-148" below.
- **Addendum (SPEC-v3 §9, 2026-10-11):** `tune.read` result gains `fused: {cpu: {word, ratio, v}}` (cmd `0x09 P1=0x11 P2=0`, one per P-core +
  one per E-cluster, guarded reads) and `turbo: {P: {ratios: [8], counts: [8]}, E: {...}, favored: {cpu: ratio}}` (plain rdmsr 0x1AD/0x1AE/
  0x650/0x651 + 0x1C read). `tune.plan` result gains per unit/point `effective_v` (baseline measured VID + offset, after the F4 clamp) +
  `clamped_by: point|null`, `vccia: {predicted_v, set_by: {unit, ratio}}` (F6), warnings (F4/F8); `REFUSED TURBO_ORDER` (F5). Curve points gain
  `reason` for `unreachable`: `"max_turbo"` | `"cpufreq_granularity"` (old records) | `"cap_granularity"` | `"active_cores"` | `"turbo_off"` | `"user_cap"` (G-132, below). A plan's prediction uses the newest `baseline` curve.
- **Addendum "G-132" (measure from R-001, `docs/knowledge/power-profile-capping.md`):** `vf.measure` **pins** every cap: `scaling_min_freq` = `scaling_max_freq` = cap on all CPUs of the
  unit (max first when raising, min first when lowering; the kernel refuses min > max), journals and restores both (journal attrs `scaling_max_freq` + `scaling_min_freq`; a journal of the
  previous version with an EPP entry is still restored). Profile, governor and EPP are never written. The cap order (`ratio x 100 MHz`, then `+1` bin) and the helper's settle are unchanged.
  Curve point fields added: `cap_khz` (int, the cap that ran a point the nominal caps missed; only then), `retried` (bool, a retry or the cap search ran), `neighbours: [A|null, B|null]` (on
  `cap_granularity`: the nearest ratios below / above that the caps did run), `fused_point` (also on unreachable V/F points), `from: [ratioA, ratioB]` and `exact: true` (on `interpolated`).
  Point `status` gains `"interpolated"`: a V/F point (`fused_point` set) with `reason` `cap_granularity` / `cpufreq_granularity` / `active_cores` that has a measured (`ok`) V/F point on both sides gets
  `vid_v` = the straight line between the nearest measured V/F points `from`, clamped into their voltages (`exact` when both are equal); `vid_min`/`vid_max` stay null, `kept`/`total` are those of the
  failed attempt. Never `ok`; points above the unit's reach (`max_turbo`, `turbo_off`, `user_cap`) or without a neighbour on one side stay `unreachable`. New `reason`s: `cap_granularity` (replaces
  `cpufreq_granularity` for new records: no cap reaches the ratio; the cap search is bounded to about 10 s per point), `turbo_off` (`intel_pstate/no_turbo` = 1: points above the unit's base ratio,
  `base_frequency` of its first CPU else `cpuinfo_max_freq`, are not attempted; `kept = total = 0`; the result's `problems[0]` is "Turbo is off (power-saver profile?): ratios above base cannot be
  reached — switch to Balanced or Performance"), `user_cap` (the user's own `scaling_max_freq` is below the ratio; was `max_turbo`). `curves.list` result gains `no_turbo: bool | null` (sysfs
  read; `null` = unknown) so the Curves page shows the turbo note before a run. `stress.start`: under the `performance` governor the EPP write is skipped (one `helper:` line), any other EPP
  failure is one aggregated `helper:` line with the CPUs as ranges.
- **Addendum "G-116" (stress / validation; SPEC §9 R7/R10/R11/R27/R28; module `helper/ops_stress.py`, `stress/`):** no mailbox, no MSR write, not write-gated.
  - **`stress.start {kind: "cpu"|"memory", method?: "integer"|"vector-math"|"cache" (cpu; default "integer"), units?: [unit] (cpu; default all 12), ratio_cap?:
    int 8..60|null, duration_s: 30..3600 (required), memory_mb?: 256..(MemAvailable-2048) (memory; default 3/4 of that)}`** (long; state `stressing`) ⇒
    `{id, kind, started, ended, duration_s, elapsed_s, stopped_by: "time"|"cancel"|"error", errors: int, error_lines: [str ≤ 20, each ≤ 200 printable],
    achieved: {unit: {mhz_mean, ratio_mode}}, cap_valid: bool|null, max_temp_c, max_pkg_w, mce_new: int, health: "clean"|"delta", record_path}` (`id` and `record_path` are `null` if the store refused the file; the reason is in `error_lines`).
    `duration_s` is the requested length, `elapsed_s` the actual one. `stopped_by`: `cancel` after `stress.stop` / `cancel` / helper exit; `error` for any
    error count, a non-zero exit of the tool, or a tool the helper had to stop after `duration_s + 20 s`; else `time`. **No "pass" field or wording (D-019):**
    `errors == 0` and `health == "clean"` is all that is said. `error_lines` = helper notes (prefix `helper:`, `kernel:`) first, then the tool's own error lines.
    `achieved` = telemetry samples after a 3 s settle, only the units that ran; `mhz_mean` from the sampler's APERF/MPERF reading, `ratio_mode` from
    `0x198[15:8]`; `null` figures (`max_temp_c`, `max_pkg_w`, empty `achieved`, `cap_valid`) when no telemetry sampler runs (the dashboard keeps one running).
    `cap_valid`: `null` without a cap or without data; else true iff every unit's `ratio_mode` is within 1 bin of `min(ratio_cap, unit max ratio)`.
    `health`: `delta` if the health snapshot differs (`health.diff`), `mce_new > 0`, or the kernel log / snapshot could not be read (never called clean unread).
    `progress` event every 2 s: `{event: "progress", op_id, elapsed_s, of_s, errors}`. Cancel is a result with `stopped_by: "cancel"`, not `CANCELLED`.
    Errors: `BAD_REQUEST` (args; `memory_mb` out of range ⇒ `detail.check = "MEM_RANGE"`), `BUSY` (any other op running), `REFUSED` with `detail.check` in
    `NO_INVOKER` (no/unknown/root invoker: the socket peer uid since G-144; `PKEXEC_UID` on the stdio dev path only), `TOOL_MISSING`, `TRUST_PATH`, `MEM_RANGE` (too little memory), `NO_CPUFREQ`, `STALE_JOURNAL`, `CAP_WRITE`,
    `SPAWN`; `INTERNAL` `RESTORE_FAILED` if cpufreq could not be put back (journal kept; the result is stored first).
  - **`stress.stop {}`** ⇒ `{cancelled: bool, state, op_id?}` (cancels only a running `stress.start`; otherwise `cancelled: false`). **`stress.list {}`** ⇒
    `{results: [summary row]}` newest first, at most 50: `{id, kind, started, ended, duration_s, elapsed_s, stopped_by, errors, cap_valid, health, mce_new,
    max_temp_c, max_pkg_w, method, units, ratio_cap, memory_mb}`. Stored `/var/lib/arltuner/stress/<id>.json` (id = curve id shape), root 0700 / 0600, contract 8
    file rules, at most 200 kept (oldest deleted); the file is the result plus `method, units, ratio_cap, memory_mb, command, returncode`.
  - **Child:** `/usr/bin/stress-ng` (cpu: `--cpu N --taskset LIST --cpu-method int64|matrixprod --verify --metrics-brief --timeout Ts`; `cache`: `--cache N` and no
    method) or `/usr/bin/stressapptest` (`-s T -M MB -W`; memory runs on all CPUs, `ratio_cap` applies to all); `realpath` trust-checked before every spawn; run **as
    the invoking user** by `Popen(user=uid, group=gid, extra_groups=[], start_new_session=True)` (setgroups/setgid/setuid in the child in C, no `preexec_fn`:
    unsafe in a threaded process); `cwd=/`, env `PATH=/usr/sbin:/usr/bin LC_ALL=C` only, stdin null, stdout+stderr piped and parsed; killed (group TERM, then KILL; only while the leader is alive and unreaped, `poll() is None` right before each `killpg`, so a reused pgid is never signalled) on
    stop, helper exit and timeout. **Ratio cap** = the measure journal (`/run/arltuner/measure-journal.json`): `scaling_max_freq = cap×100000` and EPP `performance`
    on the selected CPUs, journaled first, restored in every path; a stale journal is applied before a capped start (`STALE_JOURNAL` if it cannot be) and by the
    helper at its start. Server `OPS`: `stress.start` (long), `stress.stop`, `stress.list`. `--sim`: simulated children and telemetry, 12× faster than real time.
- **Addendum (G-119):** `tune.apply` result gains `msr_writes` (plain-MSR turbo-table writes); `ops_used` includes them. A turbo-only profile
  (`[turbo]` table, no core/point entries) is valid.
- **Addendum "G-133" (live policy limits, D-036/D-039; `helper/ops_tune.py`):** `tune.read` and `tune.last` results gain `limits` (no mailbox, no MSR: the bound policy with `"auto"` resolved, recomputed by `tune.last` from
  the policy file as it is now): `{offset_mv: {P|E: {min, max}} (int mV; min = effective_offset_floor_mv - assumed_bios_offset_mv[kind], max = offset_ceiling_mv), v_target_max_v: {P|E: float|null}
  ("auto" = the code sanity bound of the kind: tune.read does not read cmd 0x28, the guard checks the CPU's own maximum at apply; null = the policy states none = no raise), ratio_max: {P|E: int|null} (policy number, or
  "auto" = the fused 0x1C values read this boot, kept with the stored read so a helper restart resolves it too; null = none / not read), source: {ratio_max, v_target_max: {P|E: "policy"|"auto"|"none"}}}`; `limits: null` plus
  `limits_note` when the policy cannot be loaded (never an error of the read). A stored read also keeps `favored_words` `{cpu: "0x..."}` (record top level). The page reads these and nothing else for the dropdown windows and
  ceilings (no literal in `web/app.js`; only the 5 mV / 5 mV grids); without `limits` its value controls are disabled with one reason. Optional `hw_limits` (G-137, D-039) `{P|E: {ratio_max_oc, v_target_max_mv, offset_mv: [lo, hi]}}`
  adds "your CPU allows ..." to the tooltips. Page model: per unit `stockMax` (P: min(`turbo.favored[u]`, top fused ratio); E: top fused ratio) and `fusedTop` (`fused[cpu].ratio`); an OC ratio `r` is none (`r <= stockMax`), a
  fused-point ratio (`r <= fusedTop`, offset only) or an OC point (`r > fusedTop`: the only case with an adaptive voltage). Tests: `tests/helper/test_g133_limits.py`, `tests/web/test_g133_page.py` (fixture
  `tests/web/fixtures/tune-last-285k-2026-10-12.json`).
- **Addendum "G-123" (UX continuity; `helper/ops_tune.py`, `profiles/lastread.py`, `profiles/contents.py`):** no mailbox, no MSR, not write-gated; both ops are `slow` (own thread).
  - **`tune.last {}`** ⇒ `{available: false}` or `{available: true, boot_id, at: <epoch s>, applied_at: <epoch s>|null, result: <the tune.read result>}`. A *complete* `tune.read`
    (all 12 units, known boot id) is stored `<state>/last-read.json` (`<state>` = `dirname(audit)/state` = `/var/lib/arltuner/state`; **dir 0700, file 0600**, created
    `O_EXCL|O_NOFOLLOW` as a temp file, `fsync`, `rename` over the final name (a planted symlink is replaced, never followed); read `O_NOFOLLOW|O_NONBLOCK`, descriptor
    checked: regular, 1 link, helper-owned, no group/other write; ≤ 256 KiB; the directory must be helper-owned and not group/world-writable, else the file is ignored
    and logged, never an error). The record `{version: 1, boot_id, at, applied_at, words: {cpu: ["0x…"…]}, result}`; `words` is authoritative: after a `tune.apply` that wrote
    (`writes_sent > 0`) the verified read-back words replace those of the stored read and `applied_at` is set; `tune.last` then overlays them on `result.units[*].cpus[*]`, rebuilds
    `tune`, empties `differences` (`differences_stale: true`) and recomputes `accepted_match`. `available` only when the stored and the current boot id are both known and equal;
    a subset read (`cpus` given) is not stored. **Lazy restore:** the first tune.* call of a helper process (`_last_words`, `tune.last`) sets `helper.last_words` and the tune state's
    turbo tables / fused / favored facts from the stored read of this boot, unless this process already read; so `tune.plan` works after a helper restart without a new read.
    A read made outside the helper (CLI restore during the session) is not seen: the page shows the read's time so the operator can tell. Not restored: `tuned_this_boot` (G-122).
  - **`profiles.get {name}`** ⇒ `{name, source: "accepted"|"saved", created, contents: {rows: [{target: unit, offset: {kind: "curve", mv}|{kind: "points", mv: {ratio: mv}}|null,
    v_target_v: float|null, ratio: int|null}], turbo: {P?: [8], E?: [8]}|null}, summary: {units, points, deepest_offset_mv, oc_ratios, v_targets, turbo}}`; rows are per unit
    (P0..P7, EC0..EC3) in that order, saved P/E group rows spread over their units; an accepted file's numbered `[[point]]` entries become ratios (platform point ratios;
    a unit with a `[[core]]` offset and points ⇒ `points` over every fused ratio, the curve value under the point entries). G-148: a profile stands for itself (no chain
    fields); `complete` says whether it is a complete profile (addendum "G-148").
    Unknown name ⇒ `BAD_REQUEST detail.check = "PROFILE_NOT_FOUND"` (store errors `PROFILE_UNTRUSTED` / `PROFILE_IO`); accepted files are trust-checked as for `tune.plan`.
    `profiles.list` is unchanged (no contents). Server `OPS`: both ops, `tune.last` with no args, `profiles.get` `{name}` (`^[a-z0-9._-]{1,64}$`).
  - **Page:** `tune.last` on load (only when Tune has no words: a reconnect never wipes an editor) fills Tune and the applied chip with no mailbox op; Profiles shows the accepted profile,
    a per-profile summary, a compare table and **Load into Tune** (fills the editor; nothing is sent until Review changes then Apply); System and
    Dashboard show "Mailbox ops this boot: N" with a session sparkline and the per-boot cap as a note (D-032); the read estimate is "≈ 124–136" (112 V/F reads + 12 fused-point
    reads; `_read_extras` also sends 8 favored-ratio mailbox reads, so a read costs about 132); the default curve is the newest baseline, else the newest complete all-units curve.
  - `install.sh` creates `/var/lib/arltuner/state` 0700.
- **Addendum "G-137" (limits = policy up to the CPU's own maxima, D-039; `guard/policy.py`, `helper/ops_tune.py`):** `tune.read` reads, in the same session as the fused / favored words, cmd `0x01 P1=0` and `0x28 P1=0`
  on the first P CPU and the first CPU of the first E cluster (4 more mailbox reads), and `0x1C` on the first CPU of every E cluster (the E `"auto"` ratio ceiling resolves from it). Result gains `hw_limits`
  `{P|E: {ratio_max_oc: int|null, v_target_max_mv: int|null, offset_mv: [-1000, 999]}}` (`ratio_max_oc` = `0x01 [7:0]`, null when unread / 0 / bit 8 clear; `v_target_max_mv` = `0x28 [11:0] - 1 LSB` in whole mV; `offset_mv` = the field
  encoding); kept with the stored read, so `tune.last` and a restarted helper have it. The helper binds the policy to them (`Policy.with_hw_limits`) in `_load_policy`: every ratio / V target ceiling (planner, plan-time `POLICY_RANGE`, guard, turbo-table
  check) is the policy value clamped by them; unread = no raise (fail closed). `limits.v_target_max_v` reports the clamped live value (null while `0x28` is unread). Load-time bounds of a policy are field encodings only
  (`ENCODING_*`: ratio 1..255, V target 500..3999 mV, offset ceiling <= 999 / floor >= -1000 mV, steps 1..255 / 2..1000 mV). Every refusal names its source: `policy ratio_max P = 60 (<policy file>; your CPU allows up to 120x per cmd 0x01)`,
  `CPU limit 84x (cmd 0x01 ...)`, `field encoding`. The planner's read phase (CLI `plan` / `apply`, `tools/lnx/restore_tuning.sh`, and the helper's apply when the limit is not bound yet) ends with one `0x01` read per core kind whose ratio ceiling is set (`StepKind.READ_OC_CAP`, `oc_cap_kinds`; P on the first P target CPU, E on the first E target CPU, else the first CPU of the kind); `count_operations(..., oc_cap_reads)` counts it, and `build_plan(oc_cap_words=...)` binds the policy to what was read. A kind whose limit `tune.read` already bound costs no read. A policy with no `oc_capability_read` allow entry cannot plan a ratio raise (default deny).
  A `tune.read` costs 4 (limits) + number of E clusters (0x1C) more mailbox reads. Auto ratio ceiling = highest `0x1C[15:8]` (fused) of the kind + 3 (P 60, E 49). Tests: `tests/guard/test_hw_limits.py`, `tests/helper/test_hw_limits.py`.
- **Addendum "G-141" (backend review fixes, 2026-10-12; `helper/ops_tune.py`, `helper/ops_accept.py`, `controls/planner.py`, `mailbox/encoding.py`):**
  - **`tune.read` result gains `ops_done`, `ops_words`, `ops_extras`.** `ops_done` = every mailbox operation of the whole read (what the page shows as "N mailbox operations used" and what the
    per-boot counter counted): `ops_words` (the `vf.dump` words) + `ops_extras` (the guarded `0x09` / `0x1C` / `0x01` / `0x28` reads; the plain `rdmsr` of the turbo tables are not operations). The extras
    session is paced like `vf.dump` (a policy with `min_dwell_s > 0` no longer refuses every group); a refusal note names its reason (`DWELL`, ...), only `NOT_ALLOWLISTED` says "no allow entry".
    `progress` events carry `ops_used: null` when the boot id is unknown (the guard still caps that bucket).
  - **Hidden ceilings removed (D-039):** ratio 1..255, OC V target 500..3999 mV, turbo buckets 1..255 are the only fixed bounds of profiles (`profiles/schema.py`) and intents (`tune.plan`), taken from the one
    module `mailbox/encoding.py` (also `ENCODING_*` of the policy loader); a refusal names the field (`ratio field [7:0]`, `voltage field [19:8]`). The policy and the CPU's own limits decide the rest.
  - **Default assumed BIOS offset = 0 for P and E** (was P -50 mV); `policies/default.toml` states it. A bare `Policy` has `e_cpus = None` (unbound): `core_kind` raises; `default_m1_policy()` / `load_policy(text)`
    without a topology bind the simulated reference part on purpose, every helper path binds the live topology (the heartbeat budget too).
  - **Plan = guard for `"auto"` ratio:** `build_plan` judges each write with the `0x1C` words of the whole kind (session seed + baseline rows), as `WriteGuard` does. Applied turbo tables
    (`WRITE_MSR` read-backs) are folded into the tune state, `tune.last` and the stored read; a helper restart restores the `0x1C` words (`favored_words`) with the read, and `_last_restored` is set only when the restore is done.
  - **`profiles.accept` freshness:** the helper keeps `audit_mark` (byte position in the audit log where the words held were read, or where the last complete apply ended; stored with the read).
    `accept` refuses `STALE_READ` ("read again first") when the log shows a mailbox write (`0x11` ...) or a plain MSR write `attempt` after it, or the position is unknown; `{from: "read"}` records the read's turbo tables.
    `accept` and `profiles.fingerprint` hold the helper state machine (state `planning`) while they work: `BUSY` while a long op runs, and no long op starts meanwhile.
  - **`profiles.fingerprint {}`** (slow, no mailbox / MSR, not write-gated; one audit `result` record, `op profiles.fingerprint`): writes `fingerprint.json` ALONE into the existing accepted directory
    (atomic temp + rename; `CURRENT`, `profiles/` and `EXPECTED-words.txt` are not touched) from this boot's fused `0x09` words (`helper.fused_words`, restored from the stored read after a restart) and
    the CPU identity of `/proc/cpuinfo`. Result `{recorded: <UTC>, cpus: <n>, accepted: [profile names], fingerprint: "complete" | "incomplete: <why>"}` (incomplete is written all the same, as `accept` does:
    e.g. no CPU identity under `--sim`). Refusals (nothing changed, `REFUSED` + `detail.check`): `NO_ACCEPTED_DIR`, `NO_ACCEPTED_STATE` (no / untrusted accepted state; use `profiles.accept`), `NO_READ` (this boot's
    read lacks the `0x09` word of a unit), `AUDIT_UNAVAILABLE`, `IO`; error `BUSY` while another long op runs. Server `OPS`: `profiles.fingerprint` (not long).
  - **Small:** audit log created / tightened to 0600 (helper ops, `AuditLog`); a stale measure journal is recovered first and refuses `STALE_JOURNAL` if it cannot be restored (as stress does); `--sim` clocks start
    at the newest audit record (apply after a helper restart is no longer `DWELL` "in the future"); `tune.plan` refuses `AUDIT_UNAVAILABLE` when the per-boot count cannot be read (it was taken as 0); a failed
    Windows affinity restore after a failed access is logged and attached as a note. Tests: `tests/helper/test_g141.py` and the module tests named in the commits.

## Addendum G-147 — a residual OC point is neutralised; the favored ratio is never lowered as a side effect
- **Why:** an OC word (`0x10`/`0x11`, `P2 = 0`) survives a warm reboot and its ratio cannot be written back to 0 at runtime; only a cold boot clears it
  (`docs/knowledge/platform-arl-s.md`). A unit that holds an OC ratio above its top fused ratio while the accepted state has none keeps the V target the earlier
  sitting left in it, so it runs a voltage the accepted tuning never tested.
- **Rule (`controls/residual.py`, pure functions):** `neutralise(...)` rewrites such a unit with the ratio it already holds (never 0, never raised), the accepted offset
  field, and an adaptive V target below the accepted top fused point voltage (`neutral_target`, `TARGET_MARGIN_MV`, never more than one step limit below the held
  target). The hardware clamps an OC target below the top fused point up to it, so the unit runs the accepted voltage. A plan the guard refuses is dropped and the unit is
  reported as it is. `scan_plan` adds two reads (`0x10`, `0x09 P1=0x11`) per unit that the accepted state gives no OC ratio.
- **Compare:** `classify` / `compare_words` report a unit **EQUIVALENT** (`RESIDUAL_NOTE`: "residual OC point (cleared by a cold boot); voltages as accepted") when the
  accepted word has no OC point, the live ratio is above the top fused ratio, the offset field equals the accepted one, the target is at most the effective top point and
  every other point matches. Only a real difference is BAD. The same verdict feeds the helper's `accepted_match` (`equivalent: true`, `equivalents`), CLI `compare-accepted`
  (exit 0 with EQUIVALENT units), `restore_tuning.sh` / `.ps1` and the Dashboard comparison. The restore result gains `residual` (units rewritten).
- **Favored ratio:** a residual row sends no `0x1D` / `0x27`; the planner never sends a `0x1D` for a word whose ratio is at or below the top fused ratio and below the `0x1C[7:0]` read before. `0x1D` lowers the
  unit's maximum (`cpuinfo_max_freq`) [MEASURED lnx 2026-10-13, see `docs/knowledge/platform-arl-s.md`].
- **cpufreq cap follows:** after a complete apply that wrote a `0x1D`, `scaling_max_freq` of a CPU that sat at the old `cpuinfo_max_freq` follows the new one; a cap the user
  lowered is never raised (`controls/cpufreq.py`, shared by the helper's `tune.apply` and the CLI `apply --device` on Linux).
- **CLI:** `apply --neutralize-residual` (restore of the accepted state); G-148 makes it the behaviour of every complete profile.

## Addendum G-148 — complete, independent profiles; the accepted state is one profile restored in one shot (D-044)
- **Schema (contract 6):** `[meta] complete = true` marks a **complete** profile: exactly one `[[core]]` per unit of the CPU (each P-core, each E cluster) with
  `fused_top_ratio` (the unit's top fused ratio, `0x09 P1=0x11 [7:0]`, where it was made) and an explicit `offset_mv`; `ratio` + `oc_voltage_mv` only for an OC
  point (absent = none); a `[[point]]` for every fused point of every unit (1..n, `expect_ratio` on each, rising, the last at `fused_top_ratio`); `[turbo]` both
  tables or neither (neither = not part of the profile, left alone). Topology-free rules in `Profile.__post_init__`, "every unit once" in
  `Profile.check_complete(topology)`. Partial profiles (no `complete`) are unchanged. `profiles/complete.py`: `complete_from_words(topology, name, words, tops, tables)`
  (words = `[OC word, point 1..n]` per CPU; whole millivolts or `BuildError NOT_REPRESENTABLE`) and `overlay(base, edits, topology)`.
- **Planner / executor (contracts 3-4):** `build_plan(..., only_changed=True)` (every complete profile) writes only words that differ from the words read: a point whose
  word equals the goal, and a core in the same state (`_same_state`: the goal word is the word read, or no OC ratio / voltage asked, the core has no OC point and its
  offset is the goal's — a cold `0` stays `0`) are skipped; without a mailbox write no `0x15`. A target whose `fused_top_ratio` differs from the live `0x09` is refused
  `CHECK_FAILED` (another CPU). `apply_profile(..., auto_ramp=True)` (restore, complete profiles): when the plan needs a step beyond the policy's per-write limits it runs
  **rounds** — the D-016 next-step profile is planned and written, every word is read again (the guard measures each step from the word read in the session),
  the profile is planned again — until the profile's own plan is allowed. `planner.ramp_rounds` (ceil(|delta| / step), a whole-mV step counted one raw unit short)
  and `check_rounds` decide, after the first reads and **before the first write**, whether `rounds x worst-case ops (+ the residual scan)` fit the session cap, the
  boot budget and `MAX_RAMP_ROUNDS` (16); otherwise `NeedsRounds` (`STEP_LIMIT`), nothing sent. Results of earlier rounds carry negative step numbers.
  The G-147 residual rule works on the one accepted profile (`residual.neutralise(profile, topology, policy, vf, fused)`; no `accepted_before`).
- **Accepted state:** `CURRENT` names one profile. `profiles.accept` records `complete_from_words` of this boot's last read (+ its turbo tables when both were read)
  and the same words as `EXPECTED-words.txt`, for `{from: "read"}` and for a saved profile (complete or legacy) after the `NOT_APPLIED` check; result gains `complete`.
  `tune.plan {restore}` applies it (`_accepted_profile`); a `CURRENT` with several lines is refused `REFUSED detail.check = "ACCEPTED_CHAIN"` naming
  `accepted-migrate`; a complete profile whose units do not match the live topology / `0x09` is refused `PROFILE_TOPOLOGY`. `tune.plan` of a restore or a complete
  profile reserves `scan + rounds x (ops + residual rows)` with the rounds the last read says (`_rounds_from_last`; 1 without a read, the apply re-judges); its result
  gains `complete` and `rounds`; the preview lists only the words the last read says differ.
- **Saved profiles:** `profiles.save {name, intent}` stores `{name, created, profile: <TOML text>}`: `overlay(complete_from_words(last read), rows of intent)`;
  rows that change nothing (or none) save the read as it is. Refusals: `NO_READ` ("read first"), `STALE_READ`, `NOT_REPRESENTABLE`, `EXISTS`. A legacy
  `{name, created, intent}` record still loads ("partial (legacy)": its rows over the live words). `profiles.list` rows gain `complete` (and `chain: true` for the
  members of an older chain, with a `note`); `profiles.get` gains `complete` and drops `position` / `of` / `chain` / `cumulative`.
- **Migration:** `profiles/migrate.py` + CLI `accepted-migrate [--dir DIR] [--dry-run] [--sim]` (root, no hardware): the chain's profiles are merged
  (`_merge_profiles`, kept only here as the reference) and compared with the complete profile built from `EXPECTED-words.txt` (never a read) with the top fused
  ratios of `fingerprint.json` (else the last point of each line); the chain's turbo tables when it has both, else none. The old directory is copied to
  `accepted.bak-<UTC>/`, then `AcceptedStore.replace` swaps in `CURRENT` (one line) + the profile, `EXPECTED-words.txt` and `fingerprint.json` byte for byte.
- **Scripts / page:** `restore_tuning.sh` / `.ps1` apply the one profile with `--neutralize-residual` and refuse a chain naming the migrate command;
  `reapply_current.sh` runs `restore_tuning.sh`; `install.sh` prints a one-line hint for a chain. The Profiles page shows the one accepted profile and badges
  "complete" / "partial (legacy)"; no chain, positions or "as applied" toggle.

## Addendum G-144 — the helper as a socket service (D-043; supersedes the pkexec/polkit parts of Contract 8's transport)
Transport: the protocol of Contract 8 is unchanged (JSON lines, 64 KiB cap, ids, events); only the pipe is now a connection. **Root argv:** `--device` or
`--device --socket` (nothing else, once each, in that order); non-root runs (tests, `--sim`) may add `--socket-path FILE --helper-toml FILE`.
- **Socket:** `/run/arltuner/helper.sock` (`arltuner.helper.service.SOCKET_PATH`). Either handed over by systemd (`arltuner-helper.socket`, `LISTEN_PID`/`LISTEN_FDS`,
  fd 3; refused unless it is a listening AF_UNIX socket on a root-owned path; its mode is not judged) or made by the helper (trusted run dir, stale node removed only if
  it is a root-owned socket and the instance lock is held, bound under umask 0177, optional `chown :group` of `[access] group`, `chmod 0666`, then `listen`). One instance lock per machine as before.
  **The node is mode 0666 (2026-10-12 amendment): the peer check is the gate, a newly allowed user needs no re-login, the group is optional.** `/run/arltuner` stays root 0755.
- **Authorization per accept:** `SO_PEERCRED` uid against `helper.toml` `[access]` (`users` by name, `group` by membership; through the trust check whenever the file changed, see below; missing, empty,
  unreadable, untrusted or invalid = nobody; root only if listed in `users`). A refused peer, and a second client while one is connected, gets one line
  `{"id":null,"ok":false,"error":{"code":"REFUSED","message":…,"detail":{"check":"ACCESS_DENIED"|"CLIENT_BUSY"}}}` and is closed. The access check comes first (no leak of "busy").
- **Invoker = peer uid:** audit `operator` is `gui:uid<N>` for the connection; the stress child runs as that uid (root peer: `NO_INVOKER`). `PKEXEC_UID` is read only on the stdio dev path.
- **Lifecycle:** `Helper.start()` (lock, `tuned_this_boot`, journal recovery) once; `Helper.serve(rfile, wfile, peer_uid=, abort=)` per client; `Helper.stop()` at exit. Each
  client gets a `Connection` (writer, peer uid, operator, generation); every thread started for it (op workers, read-only op threads, heartbeat, telemetry, reader) captures it and
  emits through it, and a finished connection is `dead`, so its emits are dropped. `serve()` returns only when **all** of those threads have ended (no timeout in socket mode), and
  the service accepts the next client only after that, so a late answer, a stale peer uid or an old reader can never touch the next client's connection (a wedged read-only op
  keeps new clients on `CLIENT_BUSY` rather than being abandoned). A client leaving stops telemetry; an op in flight finishes at its next safe point (read-back / restore). Request
  ids start fresh per connection. **A client that stops reading** is dropped like one that leaves: every accepted connection has `SO_SNDTIMEO` (8 s), a timed-out send breaks the
  writer and shuts the socket down, and the op in flight completes as on a disconnect. Socket-activated helpers exit after `idle_exit_s` (default 600, 0 = never) with no client and no
  op; only an *admitted* client counts as activity (refused connects do not). SIGTERM ends the connection the same orderly way (the service waits up to 300 s; the unit's
  `TimeoutStopSec` is 300 s) and removes a socket the helper made. A manual start refuses to remove a node something still listens on (systemd's socket unit).
- **Refusal path:** `helper.toml` is cached by (device, inode, size, mtime, ctime) and re-read through the trust check when it changes; refusal log lines are limited to one per kind and
  uid per 10 s (with a count of the suppressed ones).
- **GUI:** `arltuner.web.helperlink.connect_helper()` checks the node (socket, root-owned), connects, and uses `preflight` as the handshake; a framed refusal or EOF becomes a
  `HelperConnectError` whose text names the fix. After connecting it also checks, with `SO_PEERCRED` on its own end, that the listener is root (a look-alike gets no request) (EACCES on `connect` = the node's mode is wrong, reinstall; the allowlist answer is the helper's framed `ACCESS_DENIED`). `--helper CMD …` (simulator/dev) is unchanged. Preflight check `helper_service` replaces `pkexec`/`polkit`/`polkit_agent`.

## Addendum G-154 (D-045, 2026-10-15) — the ring as one more unit
- `controls/ring.py`: walker (`0x10 P1=2` P2 0..10 on CPU 0, ends at rc `0x11`), `UNIT = "ring"`. Profiles: `[ring] oc_word` + `[[ring.point]] point/offset_mv/expect_ratio`
  (every point 1..n, the OC-created P2 = 9 included); `profiles/expected.py` parses the `ring <OC word> <points...>` line of `EXPECTED-words.txt`.
- Planner: steps `READ_RING_OC` / `READ_RING_POINT` / `WRITE_RING_POINT` after the per-core words, ordered by `controls.order` on the ring's own curve; the ring OC word is
  never written; a contradicted ring is refused as a unit (`ApplyPlan.ring_refused`, P/E steps stand).
- Contract 9 additions: `tune.read` -> `ring {cpu, oc, points, end}` + `ops_ring`; `accepted_match` entry `cpu: "ring"`; `tune.plan` warning `RING_NOT_RESTORED`;
  `tune.apply` -> `ring_refused` (list of reasons); `profiles.accept` -> `ring_points`. CLI `vf-dump` dumps the ring unless `--cpus` / `--no-ring`; `compare-accepted` judges it.
