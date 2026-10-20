# Known-good tuning state (2026-10-19, Core Ultra 9 285K, Z890 Steel Legend, BIOS: UVP off, E max 46x, 6400 MT/s)

Restore at any time:  `~/AI/arrowlake-tuner/captures/lnx/profiles/restore-good.sh`
Profile: `good-p53p55-fav-e46m120.toml` (one guarded apply; writes all P points 1-8, E point 6, P and E turbo tables).

## P-cores — per-point offsets (mV, as stored: 1/1024 V steps) and resulting VID under load
Stock VIDs = baseline measured 2026-10-11 (offsets 0). Points 1-4 (8/20/30/48x) are 0 on every core.

| Core | 53x stock | 53x offset | 53x VID | 55x stock | 55x offset | 55x VID | 56x offset | 57x offset |
|---|---|---|---|---|---|---|---|---|
| P0 (fav) | 1.216 | -49.8 | 1.166 | 1.299 | -28.3 | 1.271 | -14.6 (VID 1.371) | -75.2 (clamps to V(56x) 1.371) |
| P1 | 1.221 | -49.8 | 1.171 | 1.306 | -36.1 | 1.270 | 0 | 0 |
| P2 | 1.271 | -71.3 | 1.200 | 1.366 | -95.7 | 1.270 | 0 | 0 |
| P3 (fav) | 1.221 | -49.8 | 1.171 | 1.299 | -28.3 | 1.271 | -14.6 (VID 1.344) | -75.2 (clamps to V(56x) 1.344) |
| P4 | 1.226 | -49.8 | 1.176 | 1.319 | -48.8 | 1.270 | 0 | 0 |
| P5 | 1.261 | -60.5 | 1.200 | 1.359 | -87.9 | 1.271 | 0 | 0 |
| P6 | 1.249 | -49.8 | 1.199 | 1.346 | -76.2 | 1.270 | 0 | 0 |
| P7 | 1.271 | -71.3 | 1.200 | 1.376 | -106.4 | 1.270 | 0 | 0 |

P turbo table 0x1AD: 57 / 57 / 55 / 55 / 55 / 55 / 55 / 55 (favored 57x with 1-2 active cores, 55x all-core).
Under all-core compile the P rail reads 1.271 V (54/55x).

Tests passed: 15/15 all-core llama.cpp compile (-j24, uv-p53p55-e46, E 46x stock) · 2/2 all-core (uv-fav5675) · 1 build pinned to
P0+P3 (-j2, 804 s) · stress-ng P0+P3 120 s capped at 56x and at 57x. MCE 0 throughout.

## E-clusters — 46x (point 6) undervolt search, 3 all-core builds per step
| Offset (all clusters) | EC0 VID | EC1-3 VID | Result |
|---|---|---|---|
| 0 (stock) | 1.1575 | 1.1375 | 15/15 |
| -40 | 1.118 | 1.098 | 3/3 |
| -60 | 1.098 | 1.078 | 3/3 |
| -80 | 1.078 | 1.058 | 3/3 |
| -100 | 1.058 | 1.038 | 3/3 |
| **-120 (chosen)** | **1.038** | **1.018** | 3/3 |
| -140 (edge) | 1.018 | 0.998 | 3/3 |
| -150 | 1.007 | 0.988 | FAIL (ICE build 3, cpu 21 = EC3) |
| -160 | 0.998 | 0.978 | FAIL (ICE build 1, cpu 21 = EC3) |

E turbo table 0x650: 46 x8. EC3 (cpus 20-23) is the weakest cluster; every E failure was there.

## Not chosen
- E 49x: edge OC 1.270 V (1.210/1.240/1.255 failed on EC3); at 49x/1.27 V the shared current limit (0x64F bit 8) pulls P to 46-54x under heavy load.
- E 50x: not testable all-core — current limit (0x64F bit 8 at ~215 W) holds E at 49x and P at 48x.
## E-clusters — 48x alternative (OC point 48x, point 6 -120 mV, E turbo 48x8; 3 all-core builds per step)
| OC 48x target | VID under load | Result |
|---|---|---|
| 1.240 V | 1.243 | 1 build clean, stopped (operator: too high); P fell to 51-52x (current limit 0x64F bit 8) |
| **1.185 V (alternative)** | **1.185** | 3/3 — 20 mV above the first failure |
| 1.175 V (edge) | 1.175 | 3/3 screen, then 10-build run: 9/10, ICE build 10 on cpu 23 (EC3) — FAIL |
| 1.165 V | 1.1675 | FAIL (ICE build 1, cpu 21 = EC3) |
Profile `good-p53p55-fav-e48-1185.toml`: `restore-good.sh good-p53p55-fav-e48-1185`. Earlier 48x failures: 1.157 V without an OC point (cpu 8) and
the accepted 1.123/1.112 V (cpu 13) — both below this edge.

Runtime-only (not in the profile): PL2 295 W (BIOS 280 W), PL1 265 W.
