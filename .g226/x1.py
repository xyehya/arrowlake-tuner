import sys, tempfile
from pathlib import Path
sys.path[:0] = ["src", "."]
from tests.controls._support import *
from tests.controls.test_executor import *
for name, prof, cfg in [("off-1001", profile(p4(offset_mv=-1001)), None), ("off+1000", profile(p4(offset_mv=1000)), None),
   ("off-150", profile(p4(offset_mv=-150)), None), ("off+1", profile(p4(offset_mv=1)), None),
   ("ratio121", profile(p4(ratio=121)), None), ("ov", profile(p4(oc_voltage_mv=1400)), sim_config(initial_vf={4: P4_BASELINE.with_(ratio=58)})),
   ("ov2500", profile(p4(oc_voltage_mv=2500)), sim_config(initial_vf={4: P4_BASELINE.with_(ratio=58)}))]:
    tmp = Path(tempfile.mkdtemp()); s = make_session(tmp, config=cfg)
    try:
        r = s.apply(prof); print(name, "OK", r.ok, [hex(c) for _, c, _ in s.requests()], r.warnings[:3])
    except Exception as e:
        print(name, type(e).__name__, [(x.reason, x.detail) for x in getattr(e, 'refusals', [])], [hex(c) for _, c, _ in s.requests()])
