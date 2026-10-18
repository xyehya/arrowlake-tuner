"""Screenshot desktop UI screens with the mock bridge (orchestrator visual check). Host lnx.
Usage: python3 desktop/tools/ui_shots.py <ui_dir> <out_dir> [screen ...]   screens default: dashboard tune curves stress profiles system audit"""
import functools, http.server, shutil, sys, threading, time
from pathlib import Path
from playwright.sync_api import sync_playwright

ui, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
screens = sys.argv[3:] or ["dashboard", "tune", "curves", "stress", "profiles", "system", "audit"]
out.mkdir(parents=True, exist_ok=True)
H = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ui))
H.log_message = lambda *a, **k: None
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}/index.html?bridge=mock&motion=1"
chrome = shutil.which("google-chrome") or shutil.which("chromium")
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=chrome, headless=True)
    pg = b.new_page(viewport={"width": 1280, "height": 800})
    errs = []
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.goto(base); time.sleep(2.5)
    names = ["dashboard", "tune", "curves", "stress", "profiles", "system", "audit"]
    for i, s in enumerate(screens):
        if s in names:
            pg.keyboard.press(str(names.index(s) + 1))
        time.sleep(1.2)
        pg.screenshot(path=str(out / f"{i:02d}-{s}.png"))
        print(out / f"{i:02d}-{s}.png")
    print("console errors:", errs[:5])
    b.close()
srv.shutdown()
