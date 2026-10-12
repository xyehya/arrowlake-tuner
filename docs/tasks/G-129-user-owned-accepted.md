# G-129 — repo de-personalisation: user-owned accepted state (D-036)

- **Status:** ready · **Host:** lnx · **Tier:** T0 · **Branch:** `task/G-129-user-owned-accepted` · **Depends on:** G-128, G-130 merged

## Goal
A stranger who clones and installs gets **no** tuning, policy or capture of the reference machine. The accepted state is created by the user
in the app. The reference machine's own installed state (`/etc/arltuner/accepted`, `/etc/arltuner/policy.toml`) must keep working untouched.

1. **Accepted state is user data.** `install.sh` never writes `/etc/arltuner/accepted` from the repo (drop the "replaced from profiles/CURRENT on
   EVERY run" block and the `current_values` coupling). Fresh install ⇒ `/etc/arltuner/accepted/` absent/empty ⇒ the app says "No accepted state
   yet: save a profile as accepted (Profiles)" and Restore accepted is disabled (helper result + page). Existing accepted dir: kept as is.
   `uninstall.sh --purge` behaviour unchanged.
2. **New helper op `profiles.accept {name}`** (write op in the helper, guarded like `profiles.save`: trusted paths, root-owned, atomic swap):
   makes a saved profile (or the current read, `{from: "read"}`) the accepted state — writes `CURRENT` + the profile + `EXPECTED-words.txt` derived
   from the profile's words (same format the restore scripts and `accepted_match` read today). Audit entry. Profiles tab: "Set as accepted"
   button with a confirm dialog naming what it replaces. Tests (helper + browser).
3. **Policy seeding:** `install.sh` seeds `/etc/arltuner/policy.toml` only when absent (or `--reseed-config`) from a **generic** shipped default
   `policies/default.toml`: offsets ±200 mV, `ratio_max = "auto"`, `v_target_max_mv = "auto"`, cpus `"auto"` (G-128 semantics). Keep
   `policies/advanced-2026-10-11.toml` only if something still loads it, else move it.
4. **Author material out of the shipped tree:** move `profiles/{e-p6-split3,e49-xtu-restore,p-boot-ramp,p-step7}.toml`, `profiles/CURRENT`,
   `profiles/EXPECTED-words.txt`, `policies/session-*.toml`, `policies/example-oc-ramp.toml` to `examples/reference-285k/` with a README: "one chip's
   results (Core Ultra 9 285K, ASRock Z890), for illustration only — never apply to another CPU". Keep `profiles/stock-all.toml` if generic.
   `git rm` `captures/lnx/*` (keep the dir rule in .gitignore). Nothing in `src/` or `install.sh` may read `examples/`.
5. **Restore scripts:** `tools/lnx/restore_tuning.sh` / `reapply_current.sh` read the installed accepted state (`/etc/arltuner/accepted`) by default
   (`--from DIR` override); no repo `profiles/CURRENT`. `tools/win/restore_tuning.ps1`: remove the inline `$Expected` words of the reference
   machine; read `EXPECTED-words.txt` + profile from a `-From` directory (default `%LOCALAPPDATA%\arltuner\accepted`), refuse when absent.
6. **Docs:** README + `docs/USER-GUIDE.md`: supported = Arrow Lake-S desktop (family 6 model 198) on Linux; first session creates the accepted state;
   remove reference-machine numbers from user docs (they stay in `docs/knowledge/` as research history).
7. `packaging/selfcheck.py`: accepted dir optional (ok when absent).

## Read first
`install.sh`, `uninstall.sh`, `packaging/selfcheck.py`, `src/arltuner/helper/ops_tune.py` (accepted chain, `profiles.save`), `src/arltuner/helper/ops.py`
(`_load_accepted`, `accepted_match`), `web/app.js` (Profiles), `tools/lnx/restore_tuning.sh`, `tools/win/restore_tuning.ps1`, `docs/DECISIONS.md` D-036.

## Owns
`install.sh`, `uninstall.sh`, `packaging/*`, `profiles/*`, `policies/*`, `examples/*`, `captures/*`, `.gitignore`, `tools/lnx/*`, `tools/win/*`, `README.md`,
`docs/USER-GUIDE.md`, `src/arltuner/helper/*`, `src/arltuner/profiles/*`, `web/*`, `tests/**`.

## Acceptance
Suite green with browser tests. Install tests (tests/install) prove: fresh install leaves no accepted dir; an existing one is untouched; policy
seeded only when absent. `git grep -n "p-step7\|e-p6-split3\|EXPECTED-words" -- src install.sh web` → only generic file-name handling, no repo path.
