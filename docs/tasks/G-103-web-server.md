# G-103 — web server + security suite
- **Status:** ready (after OD-1) · **Host:** lnx · **Tier:** T0 · **Depends on:** none

## Goal
Unprivileged `ThreadingHTTPServer` per SPEC §4.2: bootstrap token file (0600) → `HttpOnly; SameSite=Strict` cookie, exact Host/Origin checks, JSON-only
POST, size caps, CSP / X-Frame-Options / Referrer-Policy / no-store, static files from a fixed manifest, SSE endpoint; talks to the helper through a
pipe abstraction (fake helper in tests).

## Read first
`docs/SPEC.md` §4.2, §4.3, §12 · `docs/review/2026-10-08-spec-v1-review.md` R1–R3, R23, R24.

## Owns
`src/arltuner/web/`, `web/` (static), `tests/web/`, `tools/arltuner-gui.py`.

## Acceptance
Security tests (SPEC §12): missing/wrong Host or Origin, cross-site simple POST, oversize body, path traversal, slow client, cookie absent, bootstrap
token reuse, plan-bound nonce replay (against the fake helper). No third-party code.
