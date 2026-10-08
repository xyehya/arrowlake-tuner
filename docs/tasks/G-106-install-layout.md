# G-106 — install layout + polkit + path migration
- **Status:** in progress · **Host:** lnx · **Tier:** T0 build; T1 install on the reference machine by the orchestrator with a go-ahead · **Depends on:** G-102

## Goal
`install.sh` / `uninstall.sh` (root, idempotent) per SPEC §4.4 / §11 and D-030: `/usr/lib/arltuner` (code + `platforms/*.toml`, precompiled with
`compileall` so `__pycache__` is root-owned), `/usr/lib/arltuner/arltuner-helper`, `/usr/share/arltuner/web`, polkit action
`io.github.arrowlake-tuner.helper` (`exec.path` = installed helper, `auth_admin`, no `_keep`, never `allow_active=yes`; the helper takes no argv but
`--device`), `/etc/arltuner/{policy.toml,helper.toml,accepted/}`, `/var/lib/arltuner/` (audit seeded from the legacy log, root 0600),
tmpfiles.d for `/run/arltuner` (0755 root) + `systemd-tmpfiles --create`; post-install trust-path self-check. `uninstall.sh` leaves
`/var/lib/arltuner` unless `--purge`.

## Read first
`docs/SPEC.md` §4.1, §4.4, §11 · `docs/ARCHITECTURE.md` contract 8 + all addenda · `docs/DECISIONS.md` D-024, D-029, D-030 · `src/arltuner/helper/config.py`,
`src/arltuner/helper/trust.py` · `src/arltuner/cli.py` (lock/audit defaults) · `src/arltuner/platform/schema.py` (`_default_path`).

## Owns
`install.sh`, `uninstall.sh`, `packaging/` (polkit action, tmpfiles.d, any templates), `tests/install/`; narrowly: the lock/audit default logic in
`src/arltuner/cli.py` (D-030 installed-detection rule, reusing `helper.trust`), the path constants in `src/arltuner/helper/config.py` (lock →
`/run/arltuner/mailbox.lock`), the path order in `src/arltuner/platform/schema.py` (installed tree first when running from it), the helper command /
web-dir defaults in `tools/arltuner-gui.py` (`pkexec /usr/lib/arltuner/arltuner-helper --device`).

## Acceptance
DESTDIR install into a temp root: file list and modes checked (root ownership cannot be verified from a user run — the real check is the T1 install's
self-check, which must exist and be tested with fake stat); idempotent re-run; uninstall/`--purge`; CLI path rule tests (absent ⇒ legacy, trusted ⇒
new, untrusted ⇒ refusal); install never runs code from the checkout as root after copying (copy, then run only the installed tree). All existing tests
green and unaltered except the CLI default-path tests the rule changes (list them). Never run `install.sh` for real, never sudo.
