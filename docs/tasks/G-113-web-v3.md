# G-113 — web v3: Dashboard + Curves + Tune + Profiles (SPEC-v3 UI)

- **Status:** ready · **Host:** lnx · **Safety tier:** T0 (fake helper / simulator only) · **Branch:** `task/G-113-web-v3`
- **Depends on:** main @ SPEC-v3 commit; builds against contract 9 via `tests/web/fake_helper.py` (G-111/G-112 run in parallel).

## Goal
Build the SPEC-v3 UI on the G-110 design: Dashboard (+ refresh slider, memory card + full timings, power-user controls), **Curves** (measure,
live-filling chart, library, chart/table views, compare/Δ, unit toggles and presets), **Tune** (read tuning words; simple/per-unit scope; offset
whole-curve/per-point dropdowns ±200 mV step 5; OC-point adaptive V target; OC ratio; prediction overlay; plan → diff modal → Apply → read-back;
Reset to stock; Restore accepted), **Profiles**, System, Audit. Use the `frontend-design:frontend-design` skill; keep G-110's visual language.

## Read first (only these)
`docs/SPEC-v3.md` (all) · `docs/ARCHITECTURE.md` contracts 8 + 9 · `web/` · `tests/web/` · `src/arltuner/web/server.py` header + `OPS`/`CSP`/`MANIFEST`.

## Owns
`web/index.html`, `web/app.js`, `web/style.css`, `tests/web/` (incl. `fake_helper.py`: implement every contract 9 op with realistic fixtures:
the memory fixture in `examples/reference-285k/notes/platform.md` §8, synthetic curves P ~0.70→1.40 V / E ~0.65→1.15 V, `vf.point` events while measuring).
**Not:** `src/` (the `OPS` server allowlist is G-111/G-112's).

## Constraints (unchanged from G-110)
Three files only; CSP: no inline script/style/handlers, no `style=` attributes, no `el.style`, no `innerHTML` with data, no eval; system fonts; inline
SVG built via DOM. Page may name only contract 8/9 ops (update `ALLOWED_OPS` / keep `FORBIDDEN_OPS` minus the ones contract 9 now allows:
`tune.plan`, `tune.apply` are allowed; `telemetry.stop`, raw `apply`/`restore`/`stress.*`/`freq.*` stay forbidden). Mailbox actions always state
"each request is a WRMSR" + op estimate; Apply only from the diff modal carrying the nonce. Unavailable values show their reason.
Dropdown ranges from contract/policy (offsets −200…+200 step 5; V target 0.900…P 1.625 / E 1.355; ratio ≤ P 57 / E 49); never free text for
voltages. Keyboard accessible; reduced motion; 1280–2560 px, sane at 900 px.

## Seeing it
Sim GUI exactly as G-110 (`--helper python3 tools/arltuner-helper.py --sim --audit <scratch> --lock <scratch>` [+ `--state-dir <scratch>` once
G-111 lands]); until then a scratch mock server over `fake_helper.py`. Headless Chrome screenshots (the extension may be disconnected). Never the
device helper.

## Acceptance
`python3 tools/run_tests.py` green; no CSP violations; screenshots of every tab + Curves while measuring + the Tune diff modal; report lists
interactions per page.

## Report-back (≤ 300 words)
IA + interactions · contract assumptions you had to make (exact) · files · test count · screenshot paths.
