# ARCHITECTURE — contracts every module and worker must honour

> Carried over 2026-10-08 from `linuxmon@b01f78a` (`~/AI/linuxmon`). Paths `docs/research/*`, `docs/tasks/T-0xx` and `captures/*` cited here live in that repo (see `docs/PROVENANCE.md`); "linuxmon" = this project's former name.

Language: **Python ≥ 3.12, standard library only** (`dataclasses`, `enum`, `typing.Protocol`, `tomllib`, `unittest`, `argparse`). No third-party dependencies (privileged code stays auditable). The mailbox layer is small and isolated so it can be ported later. Decision D-012.
Layout (src layout): `src/arltuner/<package>/`, tests in `tests/<package>/`, run all with `python tools/run_tests.py` (puts `src/` on the path, unittest discovery).
Facts live in `docs/knowledge/registers.md` — modules cite it; golden vectors in tests come from it.

```
linuxmon/
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
def arl_s_285k() -> Topology                          # from platforms/arl-s-285k.toml (G-101, arltuner.platform)
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

## Update — T-026 (`linuxmon vf-dump`: the V/F curve of every core; contracts 4 and the CLI changed)
- **CLI:** `vf-dump [--sim|--device] --policy FILE [--cpus LIST] [--all-e] [--measure] [--json FILE] [--audit FILE] [--lock FILE] [--allow-taint-bit N]`. One guarded session, one `CONFIRM`; per CPU `0x10 P1=0 P2=0` (OC word) then `P2 = 1, 2, ...` until the first rc `0x11` (expected end of a table). **The known count (P 8, E 6) is not a stop**: tables are per core (57x is the boost of the favored P-cores 0 and 3 only) and an OC ratio above the top fused ratio adds one point (`[COMMUNITY]`, SkatterBencher; operator 2026-10-02). Hard cap `P2 <= 10` (P) / `<= 8` (E): reaching it without an rc `0x11` is reported (`cap reached ..., no rc 0x11 seen`), not an error. Default set: 8 P-cores + the lowest CPU of each E-cluster (8, 12, 16, 20): **budget 124** (8 x 11 + 4 x 9), normally **112** reads; `--all-e` (16 E CPUs): budget **232**, normally **208**; `--cpus` overrides both. Any other rc (also `0x11` at `P2 = 0`), a timeout, a guard refusal / trip or Ctrl-C stops the whole dump: what was read is printed (and written to `--json`, `complete: false`), exit 3 (a non-trip guard refusal: 2). **Every request is checked before anything is sent (worst case = the caps):** an allow entry of `kind = "read"` for each `(cpu, 0x10, 0, P2)` up to the cap (a policy must allow `P2` 1..10), the CPU set, the session cap (`needed`; the refusal names the count) and the per-boot cap minus the audit log's count. The guard gets `max_operations_per_session = needed` (+1 with `--measure`). Dwell: the command waits `min_dwell_s` (+0.05 s) between reads on the guard's own clock/sleep (the guard refuses, never sleeps).
- **`--measure`** (per P-core and per E-cluster, every point ratio and the OC ratio if above the top point): cap cpufreq `scaling_max_freq` (P `ratio x 100 MHz`, E `(ratio + 1) x 100 MHz`, `[MEASURED lnx 2026-10-02]`), load all CPUs of the group (`stress-ng --cpu n --taskset`), 5 samples of MSR `0x198` on the first CPU read (`[47:32]/8192` V, `[15:8]` must equal the target, else `MISMATCH` and no voltage). `0x198` is read **through the guard** (`MsrRequest`, `msr_allow`); the caps are restored in a `finally` (also on Ctrl-C; a failed restore is a loud warning + exit 3). **Every VID is recorded with the core temperature at sampling time** (TVB moves the voltage ~1.1 mV per degree C, SkatterBencher `[COMMUNITY]`): `MeasureIO.read_temp_c(cpu)` = coretemp hwmon (`hwmon*/name == coretemp`, `temp*_label == "Core <core_id>"`, core_id from `cpuN/topology/core_id`), sysfs only because MSR `0x19C` / `0x1A2` are not in `PLAIN_MSR_READABLE`; shown as `VID V @ 63 °C` (a range if it moved), `@ n/a` when there is no sensor (E-cores may have no `Core N` label), JSON `temp_min_c` / `temp_max_c`. A ratio whose own frequency exceeds the current `scaling_max_freq` is skipped with a note. `--measure` needs `--device`; in `--sim` it is refused unless a test injects `measure_io_factory`. **Budget: `needed + 1`** in both caps, because the guard refuses even a plain read (`SESSION_LIMIT` / `BOOT_LIMIT`) once a cap is full (a plain read does not count, but is not exempt from the check). **Blocker: `0x198` is not in `PLAIN_MSR_READABLE` (`guard/policy.py`), so no loadable policy can allow it yet; `--measure` refuses (exit 2, nothing sent) until the orchestrator adds it.**
- **Module:** `src/arltuner/vfdump.py` (not `controls/`: the static rules forbid `subprocess` there; `SysfsMeasureIO` is the only place that spawns `stress-ng`, built only for `--device`). Output: a table per P-core / E-cluster (point, ratio, offset mV + raw, measured VID), the decoded OC word, a note when a table has more points than the known count, and flagged differences (offset **or point ratio** between P-cores at the same point, values grouped by CPU; between the CPUs of one E-cluster that were read; the OC ratio is not compared). `--json` carries the same data (raw words kept).
- **Simulator (contract 4):** `SimConfig.point_offsets` ((kind, point) -> raw offset), `SimConfig.cpu_point_offsets` ((cpu, point) -> raw; wins), `POINT_RATIOS_ARL_S` / `POINT_OFFSETS_ARL_S` (the measured P and E tables with the BIOS offsets); **`SimConfig.measured()` now uses them** (`SimConfig()` keeps its old defaults); `cpu_point_ratios` ((cpu, point) -> ratio, per-core tables; `measured()` gives the six non-favored P-cores the SIM-ONLY table `POINT_RATIOS_P_NONFAVORED_SIM` 8,20,30,45,50,52,54,55 unless the caller passes its own `point_ratios`); `oc_adds_point` (default False): an OC word ratio above the top fused ratio of the CPU makes `P2 = count + 1` answer with that ratio, rc `0x11` after it. No existing test changed.

## Update — T-027 (`vf-dump --measure`: robust sampling and adaptive caps)
- **Sampling:** 30 samples at 0.1 s per cap attempt (`SAMPLES`, `SAMPLE_INTERVAL_S`; `measure_group(samples=, interval_s=, min_valid=)`, no CLI flag yet). Only samples whose running ratio (`0x198[15:8]`) equals the target are kept; the VID range is over those. `ok` if at least `min_valid` (default 5) were kept, else `MISMATCH`. Text shows `[kept/total at target]` when some were dropped; JSON has `samples_kept` / `samples_total`.
- **Cap search** (replaces the fixed P `ratio` / E `ratio + 1` rule, which was ratio-dependent for E, `docs/research/lnx-first-read.md`): try `ratio x 100 MHz`; if the running ratio settles below the target, retry `(ratio + 1) x 100 MHz`; above it, or still short after the retry, is `MISMATCH`. A cap above `cpuinfo_max_freq` (new `MeasureIO.read_max_khz`) is never offered. `cap_khz` in the result is the cap of the reported attempt; a `+1` cap is noted. Caps are still restored in the `finally`. Guard budget is unchanged (plain reads do not count against the attempt cap); a retry doubles the audited `0x198` reads for that ratio.

## Update — T-028

`vf_point_offset_bounded` now accepts `P1 in {0, 2}` (core, ring) with `P2` 1..15; an allow entry may bind `p1` only to a subset of `{0, 2}` (P1 1 and 7 stay refused). `linuxmon point-offset --cpu N [--p1 0|2] --p2 N --offset-mv X [--expect-ratio R] [--sim|--device]` runs one guarded session of two operations: read `0x10 P1/P2` (baseline), then write `0x11` with only `[31:21]` replaced (the guard reads it back), and prints before / after / the revert word. The simulator models the ring point table (8,14,20,28,34,37,38,39x; rc `0x11` past point 8; point 8 starts at offset raw -77). The offset window uses the CPU's kind: with the shipped `assumed_bios_offset_mv` P = -50 the floor is -51 raw, so the ring's BIOS -77 needs a policy with `assumed_bios_offset_mv = { P = 0 }`.

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
