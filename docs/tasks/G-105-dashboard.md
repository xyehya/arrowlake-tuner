# G-105 — dashboard page
- **Status:** ready · **Host:** lnx · **Tier:** T0 · **Depends on:** G-102, G-103, G-104

## Goal
Plain HTML/CSS/JS page (no CDN, CSP-compatible, no inline script): preflight panel, live per-core table and tiles over SSE, applied-vs-accepted
status (from `vf.dump` compare with the accepted EXPECTED words), ops used / per-boot budget, read-only banner when preflight says so.

## Read first
`docs/SPEC.md` §6, §8 · `docs/ARCHITECTURE.md` contract 8 + all addenda (budget in `heartbeat`, `accepted_match` in `vf.dump`) · `src/arltuner/web/server.py`
(routes, `STATIC` manifest, CSP) · `tests/web/fake_helper.py`.

## Owns
`web/index.html`, `web/app.js`, `web/style.css` (the manifest stays these three files), `tests/web/test_dashboard_*.py`, `tests/web/fake_helper.py`
(extend it to speak the G-107 addendum).

## Constraints
CSP `script-src 'self'; style-src 'self'`: no inline script, no inline styles or `style=` attributes (use classes / CSSOM). `EventSource` for SSE.
`vf.dump` is write-gated and costs ~112–124 of the 600/boot budget: never auto-run; explicit button showing the cost and ops left; 202 + SSE
`result` flow. Read-only banner when preflight says so.

## Acceptance
Renders against the fake helper; light/dark; works at 1280 px and laptop width; no write controls in G1.
