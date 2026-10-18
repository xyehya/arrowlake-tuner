// node --test "desktop/app/ui/tests/*.test.mjs"   (G-207: golden intents, each validated by the helper's own parser; simulator fixture, no hardware)
// The validator is src/arltuner/helper/ops_tune.py canonical_intent, run by /usr/bin/python3 -I with src/ on sys.path (stdlib only, no helper started, no socket, no hardware).
import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import FIX from "../fixtures/fixtures.js";
import { modelFromRead, RING_ID } from "../lib/units.js";
import { emptyDraft, setCurve, setPoint, setRingPoint, setOcRatio, setVt, setOcOffset, setBucket } from "../lib/draft.js";
import { buildIntent, resetIntent, restoreIntent, profileIntent } from "../lib/intent.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..", "..");
const READ = FIX["tune-read"].result;
const model = (patch = {}) => modelFromRead({ ...structuredClone(READ), ...patch });
const E = ["EC0", "EC1", "EC2", "EC3"];
const row = (target, offset, v_target_v = null, ratio = null) => ({ target, offset, v_target_v, ratio });
const pts = (mv) => ({ kind: "points", mv });
const curve = (mv) => ({ kind: "curve", mv });

const PY = `
import json, sys
sys.path.insert(0, ${JSON.stringify(ROOT + "/src")})
from arltuner.helper.ops_tune import canonical_intent
out = []
for intent in json.load(sys.stdin):
    try:
        out.append({"ok": True, "canonical": canonical_intent(intent)})
    except Exception as exc:  # OpError: BAD_REQUEST text
        out.append({"ok": False, "error": getattr(exc, "message", str(exc))})
print(json.dumps(out))
`;
/** The helper's own canonical form of each intent (or its refusal text), in one python process. */
function validate(intents) {
  const r = spawnSync("/usr/bin/python3", ["-I", "-c", PY], { input: JSON.stringify(intents), encoding: "utf8", cwd: ROOT });
  assert.equal(r.status, 0, "python validator failed to run: " + r.stderr);
  return JSON.parse(r.stdout);
}
const built = (m, d) => {
  const r = buildIntent(m, d);
  assert.equal(r.error, undefined, r.error);
  return r.intent;
};

// ---- the golden intents ----------------------------------------------------------------------------------------------------------------
const m = model({ intent_features: undefined }); // a helper that reports no features (the fixture itself now carries "ring", G-208)
const mRing = model({ intent_features: ["ring"] });
const GOLDEN = {};

// one P point: the same intent the recorded tune.plan fixture was made from
GOLDEN.onePoint = {
  intent: built(m, setPoint(m, emptyDraft(), ["P0"], 53, -20)),
  want: { scope: "unit", rows: [row("P0", pts({ 53: -20 }))] },
};

// all-E whole curve
GOLDEN.allECurve = {
  intent: built(m, setCurve(m, emptyDraft(), E, -30)),
  want: { scope: "unit", rows: E.map((id) => row(id, curve(-30))) },
};

// mixed multi-select: P0 P3 EC0 EC1; 30x is on both kinds, 48x only on P, 40x only on E
{
  let d = setPoint(m, emptyDraft(), ["P0", "P3", "EC0", "EC1"], 30, -10);
  d = setPoint(m, d, ["P0", "P3", "EC0", "EC1"], 48, -25);
  d = setPoint(m, d, ["P0", "P3", "EC0", "EC1"], 40, -15);
  GOLDEN.mixed = {
    intent: built(m, d),
    want: {
      scope: "unit",
      rows: [
        row("P0", pts({ 30: -10, 48: -25 })),
        row("P3", pts({ 30: -10, 48: -25 })),
        row("EC0", pts({ 30: -10, 40: -15 })),
        row("EC1", pts({ 30: -10, 40: -15 })),
      ],
    },
  };
}

// OC ratio + adaptive target (E 48x on the 46x fused top; the automatic target is 1.155, then typed 1.16)
{
  const auto = setOcRatio(m, emptyDraft(), ["EC0"], 48);
  GOLDEN.ocAuto = { intent: built(m, auto), want: { scope: "unit", rows: [row("EC0", null, 1.155, 48)] } };
  GOLDEN.ocTarget = { intent: built(m, setVt(m, auto, ["EC0"], 1.1612)), want: { scope: "unit", rows: [row("EC0", null, 1.16, 48)] } };
  GOLDEN.ocOffset = { intent: built(m, setOcOffset(m, emptyDraft(), ["EC0"], 10)), want: { scope: "unit", rows: [row("EC0", null, 1.1663, 48)] } };
}

// turbo P and E (no scope/rows: a turbo-only intent)
{
  let d = setBucket(m, emptyDraft(), "P", 2, 56);
  d = setBucket(m, d, "E", 4, 47);
  GOLDEN.turbo = {
    intent: built(m, d),
    want: { turbo: { P: [57, 57, 56, 55, 55, 55, 55, 55], E: [48, 48, 48, 48, 47, 47, 47, 47] } },
  };
  GOLDEN.turboP = { intent: built(m, setBucket(m, emptyDraft(), "P", 0, 56)), want: { turbo: { P: [56, 56, 55, 55, 55, 55, 55, 55] } } }; // lowering bucket 0 only pulls down the buckets that were above it
}

// rows + turbo + ring together
{
  let d = setPoint(m, emptyDraft(), ["P1"], 20, -5);
  d = setBucket(mRing, d, "P", 7, 54);
  d = setRingPoint(mRing, d, 2, -5);
  d = setRingPoint(mRing, d, 9, -10);
  GOLDEN.everything = {
    intent: built(mRing, d),
    want: { scope: "unit", rows: [row("P1", pts({ 20: -5 }))], turbo: { P: [57, 57, 55, 55, 55, 55, 55, 54] }, ring: { points: { 2: -5, 9: -10 } } },
  };
  GOLDEN.ringOnly = { intent: built(mRing, setRingPoint(mRing, emptyDraft(), 2, -5)), want: { ring: { points: { 2: -5 } } } };
  GOLDEN.ringCurve = {
    intent: built(mRing, setCurve(mRing, emptyDraft(), [RING_ID], -3)),
    want: { ring: { points: Object.fromEntries([1, 2, 3, 4, 5, 6, 7, 8, 9].map((n) => [n, -3])) } },
  };
}

GOLDEN.reset = { intent: resetIntent(), want: { reset: "stock" } };
GOLDEN.restore = { intent: restoreIntent(), want: { restore: "accepted" } };
GOLDEN.profile = { intent: profileIntent("daily-1.0"), want: { profile: "daily-1.0" } };

for (const [name, g] of Object.entries(GOLDEN)) {
  test(`golden intent: ${name}`, () => assert.deepEqual(g.intent, g.want));
}

test("every golden intent validates against the helper's canonical_intent and comes back unchanged", () => {
  const names = Object.keys(GOLDEN);
  const res = validate(names.map((n) => GOLDEN[n].intent));
  names.forEach((n, i) => {
    assert.equal(res[i].ok, true, `${n}: the helper refused it: ${res[i].error}`);
    assert.deepEqual(res[i].canonical, GOLDEN[n].intent, `${n}: the helper's canonical form differs from what was sent`);
  });
});

test("the validator has teeth: it refuses fractions, unknown keys, an all-null row and a ring key out of range", () => {
  const res = validate([
    { scope: "unit", rows: [row("P0", pts({ 53: -19.5 }))] },
    { scope: "unit", rows: [row("P0", pts({ 53: -20 })), row("P0", curve(-5))] },
    { scope: "unit", rows: [{ ...row("P0", null), extra: 1 }] },
    { scope: "unit", rows: [row("P0", null)] },
    { ring: { points: { 0: -5 } } },
    { ring: { points: { 2: -5.5 } } },
    { scope: "unit", rows: [row("P0", pts({ 53: -20 }))], turbo: { P: [57, 57] } },
  ]);
  assert.match(res[0].error, /whole number of millivolts/);
  assert.match(res[1].error, /appears in two rows/);
  assert.match(res[2].error, /unknown row key/);
  assert.match(res[3].error, /changes nothing/);
  assert.match(res[4].error, /ring point number/);
  assert.match(res[5].error, /whole number/);
  assert.match(res[6].error, /8 integer ratios/);
});

// ---- builder rules ----------------------------------------------------------------------------------------------------------------------
test("empty draft: no intent (the helper would answer NOTHING_TO_DO)", () => {
  assert.deepEqual(buildIntent(m, emptyDraft()), { intent: null });
  assert.deepEqual(buildIntent(m, setPoint(m, setPoint(m, emptyDraft(), ["P0"], 53, -20), ["P0"], 53, 0)), { intent: null });
});

test("ring: never emitted, and a stored ring edit is an error, when the helper reported no ring feature (G-212)", () => {
  const d = setRingPoint(m, emptyDraft(), 2, -5);
  assert.deepEqual(buildIntent(m, d), { intent: null }, "the editor refused the edit in the first place");
  const forced = { units: { [RING_ID]: { pts: { 2: -5 } } }, turbo: {} };
  assert.match(buildIntent(m, forced).error, /ring/);
  assert.equal(buildIntent(m, forced).intent, undefined);
  assert.equal("ring" in built(mRing, setRingPoint(mRing, emptyDraft(), 2, -5)), true);
});

test("rows follow the unit order, offsets are whole mV and ratios are string keys, turbo equal to the read is left out", () => {
  const d = setPoint(m, setPoint(m, emptyDraft(), ["EC3"], 15, -10), ["P7", "P2"], 8, -5.4);
  const i = built(m, d);
  assert.deepEqual(i.rows.map((r) => r.target), ["P2", "P7", "EC3"]);
  assert.deepEqual(i.rows[0].offset, pts({ 8: -5 }));
  assert.deepEqual(Object.keys(i.rows[2].offset.mv), ["15"]);
  const sameTurbo = { units: {}, turbo: { P: [...m.turbo.P.ratios] } };
  assert.deepEqual(buildIntent(m, sameTurbo), { intent: null });
});

test("profile name rules match the helper's", () => {
  assert.match(profileIntent("Bad Name").error, /profile name/);
  assert.match(profileIntent("").error, /profile name/);
  assert.match(profileIntent("x".repeat(65)).error, /profile name/);
  const res = validate([profileIntent("a.b_c-1"), { profile: "Bad Name" }]);
  assert.equal(res[0].ok, true);
  assert.equal(res[1].ok, false);
});
