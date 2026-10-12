# 02 Install, update, reset, remove

This page covers getting the code onto the machine and what the installer puts where. It is for anyone who has met the
[requirements](01-requirements.md). Every command here is read from `install.sh` / `uninstall.sh`.

## 1. Get the code
```
git clone https://github.com/xyehya/arrowlake-tuner
cd arrowlake-tuner
```
The checkout is only ever **copied** by the installer. The installed copy is what the GUI's root helper runs; the helper refuses to run from a
user-writable path (SPEC §4.4). The restore script `tools/lnx/restore_tuning.sh` is the exception: it runs the checkout's Python with `sudo`, in isolated mode (`python3 -I`, G-142), as the
command-line path (D-031), so keep your checkout writable only by you.

## 2. Optional: run the offline test suite
```
python3 tools/run_tests.py
```
This uses a hardware simulator and fake files; it touches no hardware, needs no root and takes a few minutes. The README estimates about 3 minutes
and about 2250 tests. A green run proves the code works as designed, not that your CPU will accept a given tuning.

## 3. Install
```
sudo bash install.sh
```
Run it from the checkout, as root, through `sudo` (so the installer knows which user to allow, see below). Stop the GUI first (Ctrl+C in its terminal). A running helper is stopped by the installer itself
(the socket and service units, or SIGTERM to a helper you started by hand, waiting for an operation in flight to finish); it refuses only if a helper cannot be stopped.

What it does, in order:

| Step | Result |
|---|---|
| Copies and precompiles the Python package | `/usr/lib/arltuner/arltuner/`, platform files in `/usr/lib/arltuner/platforms/`, launcher `/usr/lib/arltuner/arltuner-helper` (0755, the service's `ExecStart`), self-check `/usr/lib/arltuner/arltuner-selfcheck`, allowlist editor `/usr/lib/arltuner/arltuner-access` |
| Copies the GUI files | `/usr/share/arltuner/web/` |
| Installs the helper units | `/etc/systemd/system/arltuner-helper.socket` and `arltuner-helper.service`. With systemd as PID 1 the **socket** is enabled and started (`systemctl enable --now arltuner-helper.socket`): systemd listens on `/run/arltuner/helper.sock` and starts the root helper on the first connection. An old polkit action file from an earlier install is removed. There is no polkit and no password prompt anywhere |
| Installs `tmpfiles.d` | `/usr/lib/tmpfiles.d/arltuner.conf`, creating `/run/arltuner` (mailbox lock directory, root, 0755) |
| Seeds the configuration **only if absent** | `/etc/arltuner/policy.toml` from `policies/default.toml`; `/etc/arltuner/helper.toml` with `allow_taint_bits = [2]` and the **allowlist** `[access] users = ["you"]`: the user who ran `sudo bash install.sh` (`SUDO_USER`), only if the allowlist is empty. Root is never added |
| Leaves your accepted state alone | `/etc/arltuner/accepted/` is never created, changed or removed by the installer. A fresh install has none: you create it in the app ([09](09-profiles-and-restore.md)) |
| Creates the state directory | `/var/lib/arltuner/` with the audit log `audit.jsonl` (0600) and root-only (0700) directories `curves/`, `stress/`, `profiles/`, `state/` |
| Seeds the audit log once | from the invoking user's own `~/.local/state/arltuner/audit.jsonl` if it exists (so the per-boot operation count survives a mid-boot install; this is the log the command line writes before the install); otherwise an empty log. An existing log is never overwritten |
| Runs a self-check | prints `arltuner self-check: all N checks passed.` (21 on the current install). The helper refuses to start if any check fails |

The installer **never applies any tuning and sends no mailbox traffic.** Everything installed is root-owned; the installer only runs Python from the
installed copy, in isolated mode with working directory `/`.

If the policy file already exists the installer prints `policy.toml: KEPT` when it differs from `policies/default.toml`, or `policy.toml: unchanged`.
Your edited policy is never overwritten (except with `--reseed-config`, below).

If the self-check fails, run `sudo bash uninstall.sh` or repair the files it names; do not start the GUI.

### Who may use the helper: the allowlist
The helper socket is connectable by every local user (mode 0666), but the helper checks the connecting process's user id (`SO_PEERCRED`) against the list in `/etc/arltuner/helper.toml`
at **every connection** and closes anything else with `ACCESS_DENIED`. Root is admitted only if listed by name (the installer never adds it). Only one client is served at a time.

```toml
# /etc/arltuner/helper.toml (root-owned)
allow_taint_bits = [2]
idle_exit_s = 600          # socket-activated service exits after this many idle seconds (0 = never)
[access]
users = ["alice", "bob"]   # user names; optional: group = "somegroup" admits that group's members
```
Add another user with `sudo bash install.sh --add-user NAME` (repeatable). An allowed user needs **no re-login and no group membership**; the change is picked up at that user's next connection. An existing
allowlist is never dropped, not even by `--reseed-config`. If nobody is allowed (for example you installed from a plain root login) the installer warns, the preflight check `helper_service` reports it, and
the GUI says how to fix it ([11](11-troubleshooting-faq.md)). What this means for security: [12 §7](12-safety-and-internals.md).

### Without systemd
The code does not depend on systemd. If systemd is not PID 1, the installer enables nothing and prints the command to run the helper yourself, as root, in a terminal or from your init system:
```
sudo /usr/bin/python3 -I /usr/lib/arltuner/arltuner-helper --device --socket
```
It creates `/run/arltuner/helper.sock` itself (it refuses to remove a node something still listens on) and serves until SIGTERM or until it has been idle for `idle_exit_s`.

### Limits: the defaults are yours to change
**Every limit is your policy, not the program's.** The table shows the *defaults* that `policies/default.toml` puts into `/etc/arltuner/policy.toml` on first install.
That file belongs to you: root-owned, edit it as root, `install.sh` never overwrites it, `--reseed-config` restores the defaults. The program hard-codes none of these
numbers (D-039). It refuses a value only when **your CPU** or the **register field** cannot take it (the right-hand column). Extreme-cooling users (liquid nitrogen, delidded
and binned chips) can therefore raise every default, up to what their CPU reports, at their own risk. Full table and the exact refusal texts: [12 §4](12-safety-and-internals.md).

| Item | Default in `policy.toml` | Hard ceiling (not editable, because it is not ours) |
|---|---|---|
| Voltage offset | -200 ... +200 mV | the 11-bit offset field: -1000 ... +999 mV |
| Adaptive (OC point) voltage target | `"auto"`: the CPU's own maximum from mailbox command `0x28`, minus one step, read live | the same `0x28` value; the 12-bit field (0.5 ... 3.999 V). Unread = no raise |
| OC ratio ceiling, P and E | `"auto"`: the fused ratio the CPU reports in command `0x1C` bits 15:8, **+ 3** (D-038: P 57 to 60, E 46 to 49) | the CPU's own OC-ratio limit from mailbox command `0x01` bits 7:0, read live; the field allows 1 ... 255. Unread = no raise |
| Per-write steps | ratio 8, voltage target 200 mV, offset 200 mV | ratio up to 255, voltage and offset up to 1000 mV |
| Assumed BIOS offset | 0 (P and E) | none |
| Operation counters | indicators only (100000 per boot and per session); no dwell | none |
| Always on, not limits | allowlist (default deny), one mailbox transaction at a time, health check, audit log, plan nonce, read-back | |

To change a limit, edit `/etc/arltuner/policy.toml` as root (for example put `E = 52` under `[ratio_max]`). A change applies from the next read or plan; no restart. The
Tune page and **System → Tuning limits** show, for each limit, your policy value next to what your CPU allows.

## 4. Update
```
# stop the GUI first (Ctrl+C in its terminal)
git pull
sudo bash install.sh
```
The installer replaces code, GUI files, the unit files and the tmpfiles file, and keeps `policy.toml`, the `helper.toml` allowlist, the accepted state, curves,
profiles, stress results and the audit log.

## 5. Reset the policy to the shipped default
```
sudo bash install.sh --reseed-config
```
This replaces `/etc/arltuner/policy.toml` with the shipped version and puts the other `helper.toml` settings (`allow_taint_bits`) back to their defaults. Anything you added (for example a fixed E-core
ratio ceiling) is lost; keep a copy first. The `[access]` allowlist always stays. It does not touch the accepted state or the audit log. `install.sh -h` prints the header comment.

## 6. Remove
| Command | Removes | Keeps |
|---|---|---|
| `sudo bash uninstall.sh` | stops and disables the helper units and removes them; removes `/usr/lib/arltuner` and `/usr/share/arltuner/web` (and the polkit action of an old install, if any) | `/etc/arltuner` (policy, `helper.toml` with its allowlist, accepted state), `/var/lib/arltuner` (audit log, curves, profiles), the tmpfiles file, `/run/arltuner` |
| `sudo bash uninstall.sh --purge --yes-reset-budget` | everything above, plus config, audit log, curves, profiles, the tmpfiles file and `/run/arltuner` | nothing |

`--purge` deletes the audit log, which **resets the per-boot operation count**. The script therefore insists on the second flag, and
`--yes-reset-budget` alone is refused. Purge right before a reboot. A plain uninstall keeps the policy on purpose: while `/etc/arltuner/policy.toml`
exists the command line still counts as "installed" and keeps using the shared audit log and lock (D-030).

## 7. Packaging and test installs
`DESTDIR=/some/dir sudo bash install.sh` installs into that tree instead of `/` (used by the tests and for packaging). It skips ownership changes,
`systemd-tmpfiles` and the live self-check. It is not a way to run the app without root.

## 8. Troubleshooting the install
| Message | Meaning |
|---|---|
| `must run as root (sudo bash install.sh)` | run with sudo |
| `an arltuner-helper process is running (pid N): stop the GUI / helper first` | a helper the installer could not stop (a DESTDIR install never stops one); stop it (`sudo systemctl stop arltuner-helper.socket arltuner-helper.service`, or SIGTERM), then re-run |
| `--add-user: root is never added to the allowlist` / `has uid 0` | root cannot be put in the allowlist; add the ordinary account instead |
| `WARNING: nobody is allowed to use the helper yet` | no invoking user was found (plain root login); run `sudo bash install.sh --add-user NAME` |
| `could not enable and start arltuner-helper.socket` | systemd refused the unit; `systemctl status arltuner-helper.socket` and `journalctl -u arltuner-helper` say why |
| `... must be Python >= 3.12` | the system `/usr/bin/python3` is too old |
| `incomplete checkout` / `is a symlink; refusing` | the checkout is damaged or contains symlinks where the installer expects files |
| `post-install self-check FAILED` | the installed tree does not satisfy the helper's trust rules; uninstall and re-install |
| `WARNING: no invoking user ... so no per-user audit log was seeded` | you ran `install.sh` as a plain root login, not via `sudo`; only matters if you already used the command line this boot (the per-boot operation count restarts from zero) |

Next: [03 First run](03-first-run.md) · Previous: [01 Requirements](01-requirements.md)
