# Reference machine results (illustration only)

One chip's results: a Core Ultra 9 285K on an ASRock Z890 Steel Legend WiFi, tuned and stability-tested by the project author in October 2026.
**For illustration only: never apply any of this to another CPU.** Every silicon sample has its own V/F curve; values that are stable here can crash or
corrupt data on your chip.

Nothing in `src/`, `install.sh`, the web page or the root helper reads this directory. The app starts with no accepted state: you create yours
(Tune, then Profiles > Set as accepted).

| Path | What it is |
|---|---|
| `profiles/CURRENT`, `profiles/EXPECTED-words.txt`, `profiles/accepted-2026-10-04.toml` | the reference machine's accepted state: one complete profile and its words (the file format the app writes itself, G-148) |
| `profiles/*.toml` (others) | the partial profiles that state was built from, once applied as a chain (`p-step7`, `e-p6-split3`, `p-boot-ramp`, `e49-xtu-restore`, `stock-all`); the tests use them as the legacy chain `accepted-migrate` converts |
| `policies/` | the session policies used while that tuning was developed (`session-*`), and an annotated example policy |
| `notes/platform.md` | the chip and board: BIOS, topology, fused words, stock registers, stock V/F curve, rails, memory (one sample, not a spec) |
| `notes/tuning-history.md` | how the accepted state was found: offsets, failed states, gate results, write sittings (one sample, not a spec) |

The tests use these files as fixtures for the profile / policy parsers.
