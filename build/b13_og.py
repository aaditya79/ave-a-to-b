"""Makes og.png, the 1200 x 630 link preview: the logo, the headline, and a small route map drawn from the
saved Prospect Heights to DUMBO example (no map tiles and no model calls).

Run from the build/ folder after save_examples.py: uv run --group build python b13_og.py
"""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
COLORS = {"fastest": "#8a94a3", "balanced": "#0039a6", "lowest_risk": "#00933c"}


def route_svg() -> str:
    saved = json.loads((ROOT / "data" / "saved_examples.json").read_text())["prospect-heights-dumbo"]
    trip = next(e["data"] for e in saved["events"] if e["type"] == "routes")
    points = [p for r in trip["routes"].values() for seg in r["line"] for p in seg]
    lats, lons = [p[0] for p in points], [p[1] for p in points]
    pad, w, h = 0.004, 440, 440
    lat0, lat1, lon0, lon1 = min(lats) - pad, max(lats) + pad, min(lons) - pad, max(lons) + pad
    scale = min(w / ((lon1 - lon0) * 0.757), h / (lat1 - lat0))  # 0.757 = cos(40.7°), so streets keep their shape
    x = lambda lon: (lon - lon0) * 0.757 * scale + (w - (lon1 - lon0) * 0.757 * scale) / 2
    y = lambda lat: (lat1 - lat) * scale + (h - (lat1 - lat0) * scale) / 2
    lines = []
    for key in ["fastest", "balanced", "lowest_risk"]:
        for seg in trip["routes"][key]["line"]:
            d = " ".join(f"{x(lon):.1f},{y(lat):.1f}" for lat, lon in seg)
            lines.append(f'<polyline points="{d}" fill="none" stroke="{COLORS[key]}" stroke-width="7" stroke-linecap="round" stroke-linejoin="round" />')
    a, b = trip["start"], trip["end"]
    pin = lambda p, letter, color: (f'<circle cx="{x(p["lon"]):.1f}" cy="{y(p["lat"]):.1f}" r="22" fill="{color}" stroke="#fff" stroke-width="4" />'
                                    f'<text x="{x(p["lon"]):.1f}" y="{y(p["lat"]) + 8:.1f}" text-anchor="middle" font-size="24" font-weight="700" fill="#fff">{letter}</text>')
    return f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}">{"".join(lines)}{pin(a, "A", "#0039a6")}{pin(b, "B", "#ff6319")}</svg>'


def skyline() -> str:
    """The same skyline drawing as the page header (static/header.html)."""
    html = (ROOT / "static" / "header.html").read_text()
    return html[html.index("<svg"):html.index("</svg>") + 6]


def page() -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
  body {{ margin: 0; width: 1200px; height: 630px; font-family: "Helvetica Neue", Helvetica, Arial, sans-serif; background: #f1f2f4; color: #0b1f3a; overflow: hidden; }}
  .bar {{ height: 96px; background: #111; display: flex; align-items: center; padding: 0 56px 6px; gap: 10px; color: #fff; position: relative; box-sizing: border-box; overflow: hidden; }}
  .bar > * {{ position: relative; z-index: 1; }}
  .bar .sign-skyline {{ position: absolute; right: 0; bottom: 6px; width: 67%; height: 78px; opacity: 0.15; z-index: 0; -webkit-mask-image: linear-gradient(to right, transparent, #000 25%); }}
  .strip {{ position: absolute; left: 0; right: 0; bottom: 0; height: 6px; background: linear-gradient(to right, #0039a6 0 11.1%, #ff6319 0 22.2%, #6cbe45 0 33.3%, #00933c 0 44.4%, #fccc0a 0 55.5%, #ee352e 0 66.6%, #b933ad 0 77.7%, #a7a9ac 0 88.8%, #996633 0); }}
  .b {{ width: 52px; height: 52px; border-radius: 50%; display: grid; place-items: center; font-weight: 700; font-size: 30px; }}
  .name {{ font-weight: 700; font-size: 40px; margin-left: 14px; }}
  .body {{ display: flex; align-items: center; justify-content: space-between; padding: 30px 56px; height: 474px; box-sizing: border-box; }}
  h1 {{ font-size: 68px; line-height: 1.05; margin: 0 0 22px; letter-spacing: -0.02em; max-width: 600px; }}
  p {{ font-size: 28px; line-height: 1.35; color: #4b5767; margin: 0; max-width: 580px; }}
  .map {{ background: #fff; border-radius: 24px; box-shadow: 0 6px 30px rgba(11,31,58,.12); padding: 10px; }}
</style></head><body>
<div class="bar">{skyline()}<div class="strip"></div><span class="b" style="background:#0039a6">A</span><span style="font-weight:700;font-size:30px">→</span><span class="b" style="background:#ff6319">B</span><span class="name">Ave A to B</span></div>
<div class="body"><div><h1>Which way should I bike?</h1><p>Three NYC routes compared by time and crash risk, with the stretches to watch out for.</p></div>
<div class="map">{route_svg()}</div></div>
</body></html>"""


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")  # the installed Chrome
        tab = browser.new_page(viewport={"width": 1200, "height": 630})
        tab.set_content(page())
        tab.screenshot(path=str(ROOT / "og.png"))
        browser.close()
    print(f"og.png: {(ROOT / 'og.png').stat().st_size / 1e3:.0f} KB")


if __name__ == "__main__":
    main()
