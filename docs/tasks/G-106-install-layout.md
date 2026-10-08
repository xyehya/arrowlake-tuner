# G-106 — install layout + polkit
- **Status:** ready · **Host:** lnx · **Tier:** T0 build; T1 install on the reference machine by the orchestrator with a go-ahead · **Depends on:** G-102

## Goal
`install.sh` / `uninstall.sh` (root, idempotent) per SPEC §4.4 / §11: `/usr/lib/arltuner`, `/usr/share/arltuner/web`, polkit action with
`exec.path`, `/etc/arltuner/{policy.toml,accepted/}`, `/var/lib/arltuner/`; migrate the audit log and lock (CLI follows); trust-path self-check.

## Acceptance
Install into a fake root in tests (DESTDIR); ownership/mode checks; uninstall leaves `/var/lib/arltuner` unless `--purge`.
