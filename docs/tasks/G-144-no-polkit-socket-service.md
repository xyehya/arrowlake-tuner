# G-144 — no polkit: the helper as a root socket service (D-043)

- **Status:** in progress · **Host:** lnx · **Tier:** T0 (worker, simulator) then T1 live (orchestrator) · **Branch:** `task/G-144-no-polkit`

Read D-043 (end of docs/DECISIONS.md), D-027/D-029 (helper protocol, hardening), docs/ARCHITECTURE.md helper sections, `packaging/*`,
`install.sh`, `uninstall.sh`, `tools/arltuner-gui.py`, `src/arltuner/web/helperlink.py`, `src/arltuner/helper/main.py`.

## Goal
1. **Helper socket mode** (`src/arltuner/helper/main.py` + a small `helper/socketserver` module): `--device --socket` listens on
   `/run/arltuner/helper.sock` (created safely: dir `/run/arltuner` root-owned 0755 as today; unlink a stale socket only if it is a socket owned
   by root; bind with a restrictive umask, then chown root:<group> and chmod 0660 — no window where it is wider). If `LISTEN_FDS`/`LISTEN_PID`
   (sd_listen_fds protocol, stdlib only) hand over a socket, use fd 3 instead. Accept **one client at a time**; a second connect gets one framed
   error line ("another client is connected") and is closed. Serve the existing protocol over the connection exactly as over stdio today.
2. **Authorization:** `SO_PEERCRED` (`socket.SO_PEERCRED`, struct `3i`) on every accept; allowed iff uid in `[access] users` (names resolved
   to uids at each check) or the uid's groups (`os.getgrouplist`) include `[access] group`; root (uid 0) may connect only if listed. Refusal:
   one framed error line + close + audit-free log line. `helper.toml` schema gains `[access]`; missing/empty ⇒ nobody (fail closed).
3. **Invoker = peer uid** everywhere `PKEXEC_UID` is used today (stress child user/group/extra groups, workdir `/run/user/<uid>`, audit
   `operator` "gui:uid<N>", `NO_INVOKER` when the peer is root). Keep `PKEXEC_UID` only for the stdio dev path (`--helper sudo -n
   PKEXEC_UID=…`) if tests need it; never trust it in socket mode.
4. **Lifecycle:** on client disconnect stop telemetry; an op in flight finishes (apply read-back, measure/stress restore), no new op starts;
   after the op the helper is ready for the next client. Idle exit after `idle_exit_s` (default 600, helper.toml) with no client and no op —
   only in socket-activated mode (a manually started `--socket` helper stays). Per-boot budget must keep coming from the audit log.
5. **D-029 argv:** the root launcher (`packaging/arltuner-helper`) accepts exactly `--device` or `--device --socket`; nothing else.
6. **systemd units** in `packaging/`: `arltuner-helper.socket` (`ListenStream=/run/arltuner/helper.sock`, `SocketMode=0660`,
   `SocketUser=root`, `SocketGroup=arltuner`, `RemoveOnStop=yes`) and `arltuner-helper.service` (`ExecStart=/usr/bin/python3 -I
   /usr/lib/arltuner/arltuner-helper --device --socket`, `Type=simple`, minimal hardening that does NOT break MSR / cpufreq sysfs / `/dev/mem`
   / `/dev/cpu/*/msr` / writes under `/var/lib/arltuner /run/arltuner /etc/arltuner`: e.g. `NoNewPrivileges=yes`, `ProtectHome=read-only`
   is NOT ok if the stress workdir is under /run/user — check; keep it minimal and documented; no `PrivateDevices`, no `ProtectKernelTunables`).
7. **install.sh / uninstall.sh:** create group `arltuner` (groupadd -r, if absent); write `[access] users = ["$SUDO_USER"]` (+ `group =
   "arltuner"`) into `/etc/arltuner/helper.toml` only when absent or when it has no `[access]` (never drop an existing allowlist; a flag
   `--add-user NAME` appends); `usermod -aG arltuner $SUDO_USER`; install the units to `/etc/systemd/system/` (or `/usr/lib/systemd/system`),
   `systemctl daemon-reload && systemctl enable --now arltuner-helper.socket` when systemd is PID 1, else print the one manual command;
   remove the old polkit action file `/usr/share/polkit-1/actions/io.github.arrowlake-tuner.helper.policy` if present; stop a running old helper.
   uninstall: disable/remove units, keep group/allowlist unless `--purge`.
8. **Self-check** (`packaging/selfcheck.py`): drop polkit action/rules checks; add: unit files present with the pinned ExecStart, socket unit
   mode/owner as above, no old polkit action file, `helper.toml [access]` non-empty. Update the "N checks" count where tests pin it.
9. **GUI** (`tools/arltuner-gui.py`, `web/helperlink.py`): default = connect to `/run/arltuner/helper.sock` (socket activation starts the helper);
   clear errors: socket missing ("run install.sh"), permission denied ("your user is not in the allowlist: sudo bash install.sh --add-user
   $USER"), refused (another client / not allowed). `--helper CMD…` unchanged for dev/sim. No `pkexec` anywhere; no polkit agent needed.
10. **Preflight** (`platform/preflight.py`): replace `pkexec`/`polkit`/`polkit_agent` with `helper_service` (socket present; connect works or
    the GUI is the connected client; peer allowed) — or whatever reads cleanly; update `web/app.js` only for check names if it hard-codes them.
11. Tests: socket server with a fake helper core (simulator): peer-cred allow/deny (users, group, root), one-client rule, disconnect during a
    long op, idle exit, LISTEN_FDS handover, socket perms at creation (no wider window), install.sh dry checks (existing install tests style),
    selfcheck, GUI connect errors. Remove polkit tests.

## Owns
`src/arltuner/helper/**`, `src/arltuner/web/helperlink.py`, `src/arltuner/web/server.py` (only if needed for the link), `src/arltuner/platform/preflight.py`,
`tools/arltuner-gui.py`, `tools/arltuner-helper.py`, `packaging/**`, `install.sh`, `uninstall.sh`, `tests/**`, `web/app.js` (check names only).
Not docs (documenter) — but write the contract in a short `docs/ARCHITECTURE.md` addendum (G-144).

## Acceptance
`ARLTUNER_BROWSER_TESTS=1 python3 tools/run_tests.py` green. Report what the orchestrator must verify live (in order).
