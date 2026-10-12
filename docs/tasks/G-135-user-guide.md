# G-135 — user guide, first full version (documenter agent)

- **Status:** in progress · **Host:** lnx · **Tier:** T0 · **Branch:** `task/G-135-user-guide` · **Agent:** `documenter` (`.claude/agents/documenter.md`)

## Goal (operator 2026-10-12)
"a nice technical one, with the factual stuff we have about overclocking and undervolting (all data facts) and how to run and use the repo
step by step with the required packages, installs, etc."

Create `docs/guide/` (GitHub-rendered, multi-page) and the documenter's memory `docs/guide/_documenter/NOTES.md`. Proposed outline (adjust and
record in NOTES.md):
1. `README.md` — index, what the tool is, supported hardware, safety summary, reading order.
2. `01-requirements.md` — CPU/board/BIOS settings, Linux kernel/msr/lockdown/Secure Boot, Python ≥ 3.12, polkit, optional packages
   (stress-ng, stressapptest; exact package names per distro only where the repo states them, else marked as an example).
3. `02-install.md` — clone, `install.sh` (what goes where, self-check, `--reseed-config`), update, uninstall.
4. `03-first-run.md` — start, polkit prompt, login link, System tab preflight, troubleshooting.
5. `04-arrow-lake-vf-primer.md` — the facts: V/F points per P-core / E-cluster, fused points, OC point, adaptive vs offset, monotonic clamp,
   interpolation between points, favored cores (CPPC, 0x1C), DLVR per P-core and per E-cluster, VccIA, turbo ratio tables, what cold vs warm
   boot reverts. All with evidence tags.
6. `05-measuring-curves.md` — Curves tab, Quick/Dense, pinning, reasons (`max_turbo`, `cap_granularity`, `turbo_off`, `user_cap`,
   `interpolated`), power profiles (R-001).
7. `06-undervolting.md` and `07-overclocking.md` — Tune tab workflows, limits (D-032, D-038), what was measured on the reference chip as
   labelled examples, risks (E-core low voltage, WHEA/silent errors).
8. `08-stability-testing.md` — Stress tab, acceptance gate used here (OCCT Linpack 60 min + SSE 60 min), MCE checks.
9. `09-profiles-and-restore.md` — Set as accepted, Restore accepted, restore scripts (Linux; Windows marked untested).
10. `10-cli-reference.md` and `11-troubleshooting-faq.md`.
11. `12-safety-and-internals.md` — guard, allowlist, one mailbox transaction at a time, audit log, op counter; links to SAFETY/ARCHITECTURE.

The Tune page is being relabelled in parallel (G-133: "All-Core"/"Per-Core", "Whole Curve Offset"/"Per V/F point Offsets", OC ratio before
adaptive voltage, adaptive voltage only with an OC ratio set; limits read live; D-038 OC headroom fused + 3). Use the new names; note in
NOTES.md that screenshots/labels must be rechecked after G-133 merges. Do not edit `docs/USER-GUIDE.md` (G-133 owns it); the orchestrator will
turn it into a pointer later.

## Owns
`docs/guide/**` only.

## Acceptance
Pages written per NOTES.md status; every command verified against the code; every hardware fact tagged with its source; NOTES.md complete.
