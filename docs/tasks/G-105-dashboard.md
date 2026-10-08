# G-105 — dashboard page
- **Status:** ready · **Host:** lnx · **Tier:** T0 · **Depends on:** G-102, G-103, G-104

## Goal
Plain HTML/CSS/JS page (no CDN, CSP-compatible, no inline script): preflight panel, live per-core table and tiles over SSE, applied-vs-accepted
status (from `vf.dump` compare with the accepted EXPECTED words), ops used / per-boot budget, read-only banner when preflight says so.

## Owns
`web/` (dashboard files), `tests/web/test_dashboard_*.py`.

## Acceptance
Renders against the fake helper; light/dark; works at 1280 px and laptop width; no write controls in G1.
