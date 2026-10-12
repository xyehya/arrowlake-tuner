# G-142 — fixes from the security review (2026-10-12)

- **Status:** in progress · **Host:** lnx (+ win scripts, untested there) · **Tier:** T0 · **Branch:** `task/G-142-security-review-fixes`

Rebase on main first (G-140 renamed `%LOCALAPPDATA%\linuxmon\` → `%LOCALAPPDATA%\arltuner\`). Record the outcome as **D-042** (append to
docs/DECISIONS.md): security review 2026-10-12; cookie isolation supersedes D-028 #1's "accepted" weight because D-033(d) returns the plan
nonce to the client; restore scripts under `-I`; health-gate dwell is a known limitation (add one paragraph to docs/SAFETY.md, finding 14).
1. **HIGH Windows driver from a user-writable path** (`tools/win/ts_driver.py:~32,~123`, `restore_tuning.ps1:~89`, `reapply_current.ps1`):
   install the carved driver into an admin-only directory (`%ProgramData%\arltuner\` with an ACL: Administrators + SYSTEM only, set via
   icacls in the script) and **re-verify DRIVER_SHA256 of the file at binPath before every `sc start`**; prefer create-service-per-run and
   delete it after. Keep "carved from the operator's own exe, never committed". Update docs/RUNBOOK-win.md.
2. **HIGH session cookie not port-isolated** (`src/arltuner/web/server.py`): bind to a random loopback address `127.x.y.z` (x,y,z random,
   avoid .0/.255; fall back to 127.0.0.1 only if the bind fails, with a log line). Cookies are host-scoped ⇒ separate jar. Update every
   place that assumes `127.0.0.1`: the exact-Host check, the launcher form action, `--open`, `tools/screenshots.py` origin check (accept
   `http://127.0.0.0/8:PORT`, still loopback-only), the web test harness. Also add (finding 6) a **server-side read-only session scope**:
   `/bootstrap` accepts `scope=read` (the launcher stays full); a read session may call only the read-only ops (the same set the screenshot
   tool uses); `tools/screenshots.py` logs in with `scope=read`.
3. **MED restore scripts run user-writable code as root with the user site** (`tools/lnx/restore_tuning.sh:~50`, `reapply_current.sh:~34,41`,
   `orchestrator_confirm.py`): run python with `-I`; make sure `tools/arltuner.py` adds `src/` from `__file__` (since `-I` implies `-P`). Note
   the proper follow-up (restore from the installed root-owned tree) in docs/ROADMAP.md.
4. **MED Windows policy/accepted/audit user-writable** (`tools/win/restore_tuning.ps1:~46`): keep policy + accepted state in the admin-only
   `%ProgramData%\arltuner\` (ACL); the RESTORED verdict from the live read-back, not from the audit file. Untested on Windows — say so.
5. **MED helper pipes reopenable via /proc** (`tools/arltuner-gui.py:~75`): use `socket.socketpair()` instead of pipes for the helper link
   (check `web/helperlink.py` framing), and `prctl(PR_SET_DUMPABLE, 0)` via ctypes in the GUI process. Works for pkexec and `--helper sudo …`.
6. **LOW caps**: profile count/bytes cap in `profiles/store.py:~77` (e.g. 500 profiles, 64 KiB each — refuse with a clear error); audit log:
   no truncation, but document size; do not change audit semantics.
7. **LOW NaN/Infinity**: `web/server.py:~674` `json.loads(parse_constant=…)` refusing NaN/Infinity (400), `allow_nan=False` on dumps
   (`web/helperlink.py:~86`).
8. **LOW dead allowlist entries** `plan`, `apply`, `restore` in `web/server.py` OPS → remove (keep `profiles.fingerprint`, added by G-141).
9. **LOW duplicate cookie name** (`web/server.py:~504`) → refuse (401).
10. **LOW `signal_running`** (`web/reissue.py:~28-37`): match argv exactly, not a substring.
11. **LOW `install.sh:~65-77` `helper_pids`**: match the helper's exact argv.

## Owns
`src/arltuner/web/**`, `tools/arltuner-gui.py`, `tools/screenshots.py`, `tools/lnx/**`, `tools/win/**`, `src/arltuner/profiles/store.py`,
`install.sh` (helper_pids lines), `tests/web/**`, `tests/install/**`, `tests/tools_win/**`, docs/RUNBOOK-win.md, docs/SAFETY.md, docs/ROADMAP.md,
docs/DECISIONS.md (D-042 append). NOT `web/*` (orchestrator), not `src/arltuner/helper/**`, `guard/**`, `controls/**` (G-141).

## Acceptance
`ARLTUNER_BROWSER_TESTS=1 python3 tools/run_tests.py` green.
