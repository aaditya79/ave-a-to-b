"""Records docs/images/demo.gif from the saved Columbia to Union Square run, replayed in the page. No model calls:
/run and /chat are blocked. Start the app locally first, then: uv run --group build python build/demo_gif.py http://127.0.0.1:8011 docs/images/demo.gif
"""
import io, sys, time
from pathlib import Path
from PIL import Image
from playwright.sync_api import sync_playwright
BASE, OUT = sys.argv[1], Path(sys.argv[2])
FPS, W = 6, 1100
frames = []
def snap(pg, n=1):
    img = Image.open(io.BytesIO(pg.screenshot())).convert("RGB")
    img = img.resize((W, round(img.height * W / img.width)), Image.LANCZOS)
    frames.extend([img] * n)
def hold(pg, seconds):
    end = time.time() + seconds
    while time.time() < end:
        t0 = time.time(); snap(pg); time.sleep(max(0, 1 / FPS - (time.time() - t0)))
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome"); errors = []
    pg = b.new_page(viewport={"width": 1440, "height": 860}); pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.route("**/run*", lambda r: r.abort()); pg.route("**/chat", lambda r: r.abort())  # belt and braces: no model call can start
    pg.goto(BASE); pg.wait_for_function("exploreData !== null", timeout=20000); pg.wait_for_timeout(800)
    hold(pg, 1.2)
    for field, text in [("#start", "Columbia University"), ("#end", "Union Square, Manhattan")]:
        pg.click(field)
        for ch in text:
            pg.keyboard.type(ch); snap(pg) if ch in " aeiou" else None
        snap(pg, 3); pg.wait_for_timeout(700); pg.click("h1"); pg.wait_for_timeout(200); snap(pg, 2)
    pg.hover("#go"); hold(pg, 0.6)
    # The saved run, replayed at a steady pace with its banner, the way the example chips show it
    pg.evaluate("""async () => {
        const key = "columbia-union-sq", ex = (await loadSaved())[key];
        reset(false); state.trip = ex.trip; state.asked = ex.question; state.saved = { key, ...ex };
        $("savedText").textContent = `Saved example from ${savedWhen(ex.saved_at)}. It took ${Math.round(ex.seconds)} seconds when it ran.`;
        $("savedBanner").classList.remove("hidden"); $("savedMapTag").classList.remove("hidden"); $("app").classList.add("saved");
        join("Lead", "Lead", "Lead", 1); setChip("Lead", "running"); source = { close() {} };
        window.__events = ex.events.slice(); window.__seconds = ex.seconds;
    }""")
    t_end = time.time() + 6.5; n = pg.evaluate("__events.length")
    while pg.evaluate("__events.length"):
        left = max(0.05, t_end - time.time())
        k = max(1, round(pg.evaluate("__events.length") / (left * FPS)))
        pg.evaluate(f"() => {{ for (let i = 0; i < {k} && __events.length; i++) onEvent(__events.shift()); }}")
        snap(pg); time.sleep(0.05)
    pg.evaluate("""() => $("timer").textContent = `${Math.floor(__seconds / 60)}:${String(Math.round(__seconds % 60)).padStart(2, "0")} when it ran`""")
    hold(pg, 2.2)
    # Click the fastest route's card, then a stretch to avoid
    pg.locator("#routeCards .card.route").last.scroll_into_view_if_needed(); pg.wait_for_timeout(300)
    pg.locator("#routeCards .card.route").last.click(position={"x": 30, "y": 20}); hold(pg, 2.0)
    btn = pg.locator("button", has_text="Avoid this stretch").first
    btn.scroll_into_view_if_needed(); hold(pg, 0.8)
    btn.click(); pg.wait_for_selector(".avoid-out button.linkish, .avoid-out .route-line, .avoid-out:not(:empty)", timeout=15000)
    pg.wait_for_timeout(600); hold(pg, 3.0)
    print("frames:", len(frames), "seconds:", round(len(frames) / FPS, 1), "errors:", errors)
    b.close()
pal = frames[len(frames) // 2].quantize(colors=128, method=Image.MEDIANCUT)
q = [f.quantize(palette=pal, dither=Image.NONE) for f in frames]
q[0].save(OUT, save_all=True, append_images=q[1:], duration=int(1000 / FPS), loop=0, optimize=True)
print(OUT, round(OUT.stat().st_size / 1e6, 1), "MB")
