"""Tune screen walk-through with the mock bridge (orchestrator visual check). Host lnx. Usage: /usr/bin/python3 desktop/tools/tune_shots.py <out_dir>"""
import functools, http.server, shutil, sys, threading, time
from pathlib import Path
from playwright.sync_api import sync_playwright

ui = Path(__file__).resolve().parents[1] / "app" / "ui"
out = Path(sys.argv[1]).resolve(); out.mkdir(parents=True, exist_ok=True)
H = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ui)); H.log_message = lambda *a, **k: None
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}/index.html?bridge=mock&motion=1#tune"
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=shutil.which("google-chrome"), headless=True)
    pg = b.new_page(viewport={"width": 1440, "height": 900})
    errs = []
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: errs.append("pageerror: " + str(e)))
    pg.goto(base); time.sleep(2)
    btn = pg.get_by_role("button", name="Read CPU state")
    if btn.count(): btn.first.click(); time.sleep(3)
    pg.screenshot(path=str(out / "t1-cores.png")); print(out / "t1-cores.png")
    tile = pg.locator("text=P0").first
    if tile.count(): tile.click(); time.sleep(1)
    pg.screenshot(path=str(out / "t2-p0.png")); print(out / "t2-p0.png")
    tab = pg.get_by_role("tab", name="Turbo ratios")
    if not tab.count(): tab = pg.locator("text=Turbo ratios")
    tab.first.click(); time.sleep(1.5)
    pg.screenshot(path=str(out / "t3-turbo.png")); print(out / "t3-turbo.png")
    print("console errors:", errs[:8])
    b.close()
srv.shutdown()
