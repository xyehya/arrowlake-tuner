# G-121 — stress fixes from the first real run

- **Status:** ready · **Host:** lnx · **Tier:** T0 · **Branch:** `task/G-121-stress-hw-fixes`

## Goal
First real `stress.start` (2026-10-11 20:01 UTC) ended at once: exit 1, `elapsed_s 0.1`. Reproduced: run as the user with cwd `/`, stress-ng prints
`aborting: temp-path '.' must be readable and writeable` and exits 1 [MEASURED lnx 2026-10-11]. Fix:
1. Give the child a private working/temp dir owned by the invoking user: prefer `/run/user/<uid>` if it exists, is a directory owned by that uid
   with mode 0700 (lstat, no symlink); else create a fresh 0700 dir under `/tmp` (`mkdtemp`, then chown to uid/gid before exec) and remove it after.
   Pass it as `cwd` and to stress-ng as `--temp-path <dir>`; stressapptest gets the same cwd.
2. On an abnormal exit, put the child's last non-empty output lines (≤ 5, trimmed to 200 chars) into `error_lines` after the helper line, so the
   page shows the real reason.
3. Tests: fake child that aborts with that exact message ⇒ error_lines carries it; dir choice + ownership checks with injectable lstat/chown.

## Read first
`src/arltuner/helper/ops_stress.py` · `src/arltuner/stress/` · `tests/helper/test_stress.py`

## Owns
`src/arltuner/helper/ops_stress.py`, `src/arltuner/stress/`, `tests/stress/`, `tests/helper/test_stress.py`.
