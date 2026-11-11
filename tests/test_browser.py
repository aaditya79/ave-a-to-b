"""The page in a real browser: typing in From shows suggestions. Needs Playwright and Chrome (uv sync --group build)."""

import json
import socket
import threading
import time

import pytest
import uvicorn

sync_api = pytest.importorskip("playwright.sync_api")

import app


@pytest.fixture(scope="module")
def base_url():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def page(base_url):
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome")
        except Exception:
            browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.errors = []
        page.on("pageerror", lambda e: page.errors.append(str(e)))
        page.goto(base_url)
        yield page
        browser.close()


def test_the_page_script_runs_without_errors(page):
    page.wait_for_timeout(500)
    assert page.errors == []
    assert page.evaluate("typeof autocomplete === 'function' && typeof avoidStretch === 'function'")


def test_typing_shows_suggestions_and_keyboard_picks_one(page):
    page.click("#start")
    page.keyboard.type("Union", delay=50)
    page.wait_for_selector("#start-suggestions li", timeout=5000)
    assert "Union Square" in page.locator("#start-suggestions li").first.inner_text()
    assert page.locator("#start-suggestions li").first.locator(".landmark").inner_text() == "Landmark"
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    assert page.input_value("#start") == "Union Square, Manhattan"
    assert page.locator("#start-suggestions").is_hidden()


def test_outside_click_closes_and_the_next_field_is_clickable(page):
    page.fill("#start", "")
    page.click("#start")
    page.keyboard.type("Colum", delay=50)
    page.wait_for_selector("#start-suggestions li", timeout=5000)
    page.click("h1")  # outside the field
    assert page.locator("#start-suggestions").is_hidden()
    page.click("#end", timeout=2000)  # nothing left on top of the next box
    assert page.evaluate("document.activeElement.id") == "end"
    assert page.input_value("#start") == "Colum"


SAVED_KEYS = ["columbia-union-sq", "prospect-heights-dumbo", "astoria-times-sq"]


@pytest.mark.parametrize("key", SAVED_KEYS)
def test_a_saved_example_always_shows_the_saved_banner(page, base_url, key):
    page.goto(base_url)
    page.click(f'.chip[data-key="{key}"]')
    page.wait_for_selector("#summary .support, #summary p", timeout=10000)
    assert page.is_visible("#savedBanner") and page.is_visible("#savedMapTag")
    banner = page.inner_text("#savedBanner")
    assert banner.startswith("SAVED EXAMPLE") or banner.lower().startswith("saved example")
    assert "Saved example from" in banner and "Run a fresh one" in banner
    assert page.locator(".card.route").count() >= 1  # the full result is there, not a stub
    # A shared link to the example opens it saved too
    page.goto(f"{base_url}/?example={key}")
    page.wait_for_selector("#summary .support, #summary p", timeout=10000)
    assert page.is_visible("#savedBanner")
    assert page.errors == []


def test_the_saved_banner_goes_away_when_a_new_run_starts(page, base_url):
    page.goto(f"{base_url}/?example={SAVED_KEYS[0]}")
    page.wait_for_selector("#savedBanner:not(.hidden)", timeout=10000)
    page.evaluate("reset(false)")  # what a live run does first
    assert page.is_hidden("#savedBanner") and page.is_hidden("#savedMapTag")
    assert not page.evaluate("document.getElementById('app').classList.contains('saved')")


# --- The shared header, Popular rides, and the header links ---

def header_facts(page):
    return page.evaluate("""() => {
        const bar = document.querySelector(".signbar"), color = (sel) => getComputedStyle(document.querySelector(sel)).backgroundColor;
        return { a: color(".sign-bullet.sign-a"), b: color(".sign-bullet.sign-b"), top: bar.getBoundingClientRect().top,
                 borderTop: getComputedStyle(bar).borderTopWidth, atTop: !!document.elementFromPoint(20, 0)?.closest(".signbar"),
                 links: [...document.querySelectorAll(".sign-links a")].map((a) => a.textContent.trim()),
                 current: [...document.querySelectorAll(".sign-links a[aria-current=page]")].map((a) => a.textContent.trim()),
                 skyline: !!bar.querySelector(".sign-skyline"), strip: getComputedStyle(bar.querySelector(".sign-strip")).height,
                 html: bar.outerHTML.replace(/ aria-current="page"/g, "") };
    }""")


def test_every_page_has_the_same_header(page, base_url):
    page.goto(base_url)
    main = header_facts(page)
    page.goto(base_url + "/how-we-calculate-risk")
    method = header_facts(page)
    for facts in (main, method):
        assert facts["a"] == "rgb(0, 57, 166)" and facts["b"] == "rgb(255, 99, 25)"  # blue A, orange B
        assert facts["links"] == ["Explore the city", "Popular rides", "History", "How we calculate risk"]
        assert facts["top"] == 0 and facts["atTop"] and facts["borderTop"] == "0px"  # no line or gap above it
        assert facts["skyline"] and facts["strip"] == "4px"
    assert main["html"] == method["html"]  # the same header markup on both pages
    assert method["current"] == ["How we calculate risk"] and main["current"] == []
    page.goto(base_url)


def test_a_ride_opens_below_the_grid_and_closes_three_ways(page, base_url):
    page.goto(base_url)
    page.wait_for_selector(".ride")
    heights = page.evaluate("[...document.querySelectorAll('.ride')].map((c) => c.getBoundingClientRect().height)")
    assert len(set(heights)) == 1  # an even grid
    card = page.locator(".ride").first
    paths = "document.querySelectorAll('.leaflet-overlay-pane path.leaflet-interactive, .leaflet-overlay-pane path').length"
    for close in ("again", "x", "escape"):
        card.click()
        assert page.locator("#rideDetail").is_visible() and card.get_attribute("aria-expanded") == "true"
        assert page.evaluate("[...document.querySelectorAll('.ride')].map((c) => c.getBoundingClientRect().height)") == heights
        if close == "again":
            card.click()
        elif close == "x":
            page.click("#rideDetail .close")
        else:
            page.keyboard.press("Escape")
        assert page.locator("#rideDetail").is_hidden() and card.get_attribute("aria-expanded") == "false", close
        assert page.evaluate("rideLayer.getLayers().length") == 0, close


def test_explore_link_goes_back_to_the_start(page, base_url):
    page.goto(base_url + "/?example=columbia-union-sq")
    page.wait_for_selector("#summary .support")
    page.locator(".ride").first.click()
    page.click("#navExplore")
    page.wait_for_timeout(400)
    assert page.is_checked("#exploreOn") and page.evaluate("location.pathname + location.search") == "/"
    assert not page.evaluate("document.getElementById('app').classList.contains('has-results')")
    assert page.evaluate("rideLayer.getLayers().length") == 0 and page.locator("#rideDetail").is_hidden()
    assert page.locator("#savedBanner").is_hidden()


# --- Loops, History, calories, and the GPX button (no model call in any of them) ---

def count_model_requests(page):
    calls = []
    page.on("request", lambda r: ("/run" in r.url or r.url.endswith("/chat") or "/followup" in r.url) and calls.append(r.url))
    return calls


def test_central_park_ride_here_draws_its_own_closed_loop_without_a_model_call(page, base_url):
    calls = count_model_requests(page)
    page.goto(base_url)
    page.wait_for_selector(".ride")
    page.locator(".ride", has_text="Central Park loop").click()
    page.locator("#rideDetail button", has_text="Ride here").click()
    page.wait_for_selector("#loopCard:not(.hidden)")
    line = page.evaluate("loopLayer.getLayers().find((l) => l.getLatLngs && !l.getRadius)?.getLatLngs().map((p) => [p.lat, p.lng])")
    assert line[0] == line[-1] and len(line) > 50  # closed
    assert page.locator(".loop-pin").inner_text() == "Start and finish here"
    assert page.locator(".loop-arrow").count() >= 10
    assert "counterclockwise" in page.locator("#loopCard").inner_text()
    assert page.locator("#loopCard button", has_text="Download GPX (Strava, Komoot, Garmin)").count() == 1
    assert page.locator("#loopCard .kcal").count() == 1
    assert calls == []  # no agent run
    page.click("#loopCard .close")
    assert page.locator("#loopCard").is_hidden() and page.evaluate("loopLayer.getLayers().length") == 0


def test_a_one_way_ride_here_still_fills_from_and_to(page, base_url):
    page.goto(base_url)
    page.wait_for_selector(".ride")
    page.locator(".ride", has_text="Hudson River Greenway").click()
    page.locator("#rideDetail button", has_text="Ride here").click()
    assert page.input_value("#start") == "Battery Park" and page.input_value("#end") == "George Washington Bridge"
    assert page.locator("#loopCard").is_hidden()


def saved_stream():
    import json as _json
    from pathlib import Path as _Path
    ex = _json.loads((_Path(__file__).resolve().parent.parent / "data" / "saved_examples.json").read_text())["prospect-heights-dumbo"]
    return "".join(f"data: {_json.dumps(e)}\n\n" for e in [{"type": "start", "session_id": "h"}, *ex["events"]])


def test_history_keeps_a_finished_trip_and_reopens_it_without_a_model_call(page, base_url):
    page.goto(base_url)
    page.evaluate("localStorage.clear()")
    body = saved_stream()
    # A finished trip, played back as if it ran: the page saves it to History
    page.route("**/run?*", lambda r: r.fulfill(status=200, headers={"Content-Type": "text/event-stream"}, body=body))
    page.fill("#start", "Prospect Heights, Brooklyn")
    page.fill("#end", "DUMBO, Brooklyn")
    page.click("#go")
    page.wait_for_function("localStorage.getItem('aveHistory')", timeout=15000)  # written when the run finishes
    page.unroute("**/run?*")
    saved = page.evaluate("JSON.parse(localStorage.getItem('aveHistory'))")
    assert len(saved) == 1 and saved[0]["trip"]["start"] == "Prospect Heights, Brooklyn"
    assert saved[0]["rec"]["miles"] > 0 and len(saved[0]["rec"]["line"]) > 5 and saved[0]["result"]["map"]["routes"]
    calls = count_model_requests(page)
    assert "from=" in page.url
    page.route("**/run?*", lambda r: r.abort())  # in case it did ask: never reach a model from a test
    page.reload()  # the URL still names the trip: a reload reopens it from History instead of asking the model again
    page.wait_for_selector("#summary .support")
    assert page.text_content("#savedTag") == "From your History" and "from=" in page.url
    page.unroute("**/run?*")
    page.goto(base_url)
    page.click("#navHistory")
    page.wait_for_selector(".hist-item")
    assert "miles planned" in page.inner_text("#historyTotal") and page.locator(".hist-item svg").count() == 1
    assert "kcal" in page.locator(".hist-item").inner_text()
    page.locator(".hist-item").click()
    page.wait_for_selector("#summary .support")
    assert page.text_content("#savedTag") == "From your History" and "Saved from" in page.inner_text("#savedText")
    assert page.locator("#routeCards .card.route").count() >= 2
    assert len(page.evaluate("JSON.parse(localStorage.getItem('aveHistory'))")) == 1  # a replay isn't saved again
    assert calls == []
    page.click("#navHistory")
    page.click("#historyClear")
    page.click("#historyClear")  # two taps
    assert page.evaluate("localStorage.getItem('aveHistory')") is None and "No trips yet" in page.inner_text("#historyTotal")


def test_calories_use_met_by_speed_and_the_weight_field(page, base_url):
    page.goto(base_url)
    page.evaluate("localStorage.removeItem('aveWeightKg'); localStorage.setItem('aveWeightUnit', 'kg')")
    assert page.evaluate("kcal(6, 60)") == 4 * 70 * 1  # 6 mph: MET 4, 70 kg, 1 hour
    assert page.evaluate("kcal(11, 60)") == round(6.8 * 70)  # 11 mph: MET 6.8
    assert page.evaluate("kcal(6.2, 39.2, 80)") == round(4 * 80 * 39.2 / 60)
    page.goto(base_url + "/?example=columbia-union-sq")
    page.wait_for_selector("#summary .support")
    card = page.locator("#routeCards .card.route").first  # the recommended route comes first
    assert "rough estimate" in card.inner_text() and card.locator(".kcal").count() == 1
    before = int(card.locator(".kcal").inner_text())
    card.locator(".weight-input").fill("90")
    card.locator(".weight-input").dispatch_event("change")
    after = int(card.locator(".kcal").inner_text())
    assert after == round(before * 90 / 70) or abs(after - before * 90 / 70) <= 1
    assert page.evaluate("localStorage.getItem('aveWeightKg')") == "90"
    assert page.locator("button", has_text="Download GPX (Strava, Komoot, Garmin)").count() >= 1
    page.evaluate("localStorage.removeItem('aveWeightKg')")


def test_the_cap_message_offers_saved_examples_and_history(page, base_url):
    import app as _app
    page.goto(base_url)
    page.route("**/run?*", lambda r: r.fulfill(status=200, headers={"Content-Type": "text/event-stream"},
               body='data: ' + __import__("json").dumps({"type": "error", "message": _app.DAILY_MESSAGE, "limit": True}) + "\n\n"))
    page.fill("#start", "Columbia University")
    page.fill("#end", "Union Square")
    page.click("#go")
    page.wait_for_selector(".limit-help")
    help_ = page.locator(".limit-help").inner_text()
    assert "Open History" in help_ and "Columbia" in help_
    assert "null" not in page.inner_text("#errorBox")
    page.unroute("**/run?*")


def test_example_chips_never_call_the_model(page, base_url):
    calls = count_model_requests(page)
    page.goto(base_url)
    for key in ["columbia-union-sq", "prospect-heights-dumbo", "astoria-times-sq"]:
        page.locator(f".examples .chip[data-key='{key}']").click()
        page.wait_for_selector("#savedBanner:not(.hidden)")
        page.wait_for_selector("#summary .support")
    assert calls == []


@pytest.mark.parametrize("width", [1440, 1100, 390])
def test_stat_strip_numbers_stay_level(page, base_url, width):
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(base_url)
    page.wait_for_function("document.getElementById('statCrashes').textContent !== '…'")
    boxes = page.evaluate("""[...document.querySelectorAll('#statStrip > div')].map(d => ({
        number: Math.round(d.querySelector('b').getBoundingClientRect().top),
        label: Math.round(d.querySelector('span').getBoundingClientRect().top),
        lines: Math.round(d.querySelector('span').getBoundingClientRect().height / 17) }))""")
    assert len({b["number"] for b in boxes}) == 1 and len({b["label"] for b in boxes}) == 1
    assert page.inner_text("#statStrip span") == "cyclist injury crashes since 2021"
    if width == 1440:
        assert all(b["lines"] == 1 for b in boxes)  # every label on one line at desktop width
    page.set_viewport_size({"width": 1280, "height": 800})


def test_an_error_after_routes_were_drawn_leaves_no_partial_results(page, base_url):
    import json as _json
    from pathlib import Path as _Path
    import app as _app
    ex = _json.loads((_Path(__file__).resolve().parent.parent / "data" / "saved_examples.json").read_text())["columbia-union-sq"]
    routes = next(e for e in ex["events"] if e["type"] == "routes")
    body = "".join(f"data: {_json.dumps(e)}\n\n" for e in [{"type": "start", "session_id": "b"}, routes,
                   {"type": "error", "message": _app.BUSY_MESSAGE, "retry": True, "busy": True}])
    page.goto(base_url)
    page.route("**/run?*", lambda r: r.fulfill(status=200, headers={"Content-Type": "text/event-stream"}, body=body))
    page.fill("#start", "Columbia University")
    page.fill("#end", "Union Square")
    page.click("#go")
    page.wait_for_selector("#errorBox:not(.hidden)")
    page.unroute("**/run?*")
    assert page.inner_text("#errorBox p") == _app.BUSY_MESSAGE
    assert page.locator("#retryRun").count() == 1 and page.locator("#errorBox .limit-help .chip").count() == 3
    assert page.locator("#routeCards > *").count() == 0
    assert page.evaluate("routeLayer.getLayers().length + labelLayer.getLayers().length + spotLayer.getLayers().length") == 0
    for hidden in ["tiles", "summary", "sliderBox", "progress", "traceCard", "legend"]:
        assert page.locator(f"#{hidden}").is_hidden(), hidden


def click_map_at(page, lat, lon):
    point = page.evaluate(f"(() => {{ const p = map.latLngToContainerPoint([{lat}, {lon}]); const b = map.getContainer().getBoundingClientRect(); return [b.left + p.x, b.top + p.y]; }})()")
    page.mouse.click(*point)


def test_two_map_clicks_fill_from_and_to_and_drag_updates(page, base_url):
    reverse = {"n": 0}

    def fake_reverse(route):
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(route.request.url).query)
        lat, lon = float(q["lat"][0]), float(q["lon"][0])
        reverse["n"] += 1
        if lon < -74.03:  # New Jersey
            return route.fulfill(json={"error": "That spot is outside NYC, so it can't be routed.", "outside": True})
        route.fulfill(json={"value": f"Spot {reverse['n']}, Manhattan", "lat": lat, "lon": lon, "address": True})
    calls = count_model_requests(page)
    page.goto(base_url)
    page.route("**/reverse?*", fake_reverse)
    page.evaluate("map.setView([40.75, -73.99], 13, { animate: false })")
    page.click("#pickStart")
    assert page.inner_text("#pickHint") == "Click the map to set your start"
    click_map_at(page, 40.7484, -73.9857)
    page.wait_for_function("document.getElementById('start').value === 'Spot 1, Manhattan'")
    assert page.inner_text("#pickHint") == "Click the map to set your destination"  # switched to To by itself
    click_map_at(page, 40.7359, -73.9911)
    page.wait_for_function("document.getElementById('end').value === 'Spot 2, Manhattan'")
    assert page.locator("#pickHint").is_hidden() and page.evaluate("!!pins.start && !!pins.end && !picking")
    # Outside NYC: an error, and no pin moves
    page.click("#pickEnd")
    click_map_at(page, 40.7440, -74.0400)
    page.wait_for_function("document.getElementById('pickHint').classList.contains('bad')")
    assert page.inner_text("#pickHint") == "That spot is outside NYC, so it can't be routed."
    assert page.input_value("#end") == "Spot 2, Manhattan"
    page.keyboard.press("Escape")  # cancels pick mode
    assert page.evaluate("picking") is None and page.locator("#pickHint").is_hidden()
    # Dragging a pin updates its input
    page.evaluate("pins.start.setLatLng([40.7527, -73.9772]); pins.start.fire('dragend')")
    page.wait_for_function("document.getElementById('start').value.startsWith('Spot 4')")
    page.unroute("**/reverse?*")
    assert calls == []


def test_moving_a_pin_after_results_clears_them_and_waits(page, base_url):
    calls = count_model_requests(page)
    page.goto(base_url + "/?example=columbia-union-sq")
    page.wait_for_selector("#summary .support")
    assert page.evaluate("!!pins.start && !!pins.end")  # the route's ends are draggable pins
    page.route("**/reverse?*", lambda r: r.fulfill(json={"value": "Moved, Manhattan", "lat": 40.8, "lon": -73.96, "address": True}))
    page.evaluate("pins.end.setLatLng([40.8, -73.96]); pins.end.fire('dragend')")
    page.wait_for_function("document.getElementById('end').value === 'Moved, Manhattan'")
    page.unroute("**/reverse?*")
    assert page.locator("#routeCards > *").count() == 0 and page.locator("#summary").is_hidden()
    assert page.locator("#progress").is_hidden() and calls == []  # nothing runs until Compare routes
    page.fill("#end", "Union Square")  # typing a different place removes the stale pin
    assert page.evaluate("pins.end") is None


def test_a_reset_right_after_results_draw_throws_nothing(page, base_url):
    page.goto(base_url + "/?example=columbia-union-sq")
    page.wait_for_selector("#summary .support")
    page.errors.clear()
    page.evaluate("onEvent({ type: 'routes', data: state.map }); reset()")  # the map fit is still pending when the page resets
    page.wait_for_timeout(600)
    assert page.errors == []


def test_info_buttons_have_a_24_px_tap_area(page, base_url):
    page.goto(base_url + "/?example=columbia-union-sq")
    page.wait_for_selector("#routeCards .info")
    page.locator("#routeCards .info").first.scroll_into_view_if_needed()
    size = page.evaluate("""(() => { const b = document.querySelector('#routeCards .info').getBoundingClientRect();
        const x = b.left + b.width / 2, y = b.top + b.height / 2, hit = (dx, dy) => document.elementFromPoint(x + dx, y + dy)?.closest('.info');
        return [b.width, hit(11, 0) && hit(-11, 0) && hit(0, 11) && hit(0, -11) ? 1 : 0]; })()""")
    assert size == [18, 1]  # looks 18 px wide, takes taps 11 px from its center


@pytest.mark.parametrize("width", [1440, 390])
def test_bullets_sit_10_px_from_from_and_to(page, base_url, width):
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(base_url)
    gaps = page.evaluate("""[...document.querySelectorAll('.field-head label')].map(l => {
        const b = l.querySelector('.bullet').getBoundingClientRect(), r = document.createRange();
        r.setStart(l.lastChild, 0); r.setEnd(l.lastChild, l.lastChild.length);
        return Math.round(r.getBoundingClientRect().left - b.right); })""")
    assert gaps == [10, 10]
    page.set_viewport_size({"width": 1280, "height": 800})


def lga_map():
    """The map a Manhattan -> LaGuardia trip shows, built in code (no model): five hotspots close together on
    Northern Boulevard, which is where markers once got spread far off the routes."""
    run = app.Research(hooks=app.TraceHooks(app.AgentContexts(), lambda e: None))
    app.find_routes(run, "Union Square, Manhattan", "LaGuardia Airport, Queens")
    facts = app.page_facts(run, run.hooks)
    return {**app.map_routes(run.routes), "hotspots": facts["hotspots"], "before_after": facts["before_after"]}


SPOT_CHECK = """(() => {
  const px = (p) => map.latLngToContainerPoint(p);
  const segDist = (p, a, b) => { const dx = b.x - a.x, dy = b.y - a.y, L = dx*dx + dy*dy; const t = L ? Math.max(0, Math.min(1, ((p.x-a.x)*dx + (p.y-a.y)*dy) / L)) : 0; return Math.hypot(p.x - a.x - t*dx, p.y - a.y - t*dy); };
  const lines = []; routeLayer.eachLayer((g) => g.eachLayer((l) => l.options.interactive !== false && lines.push(l.getLatLngs().map(px))));
  const near = (p) => Math.min(...lines.map((ln) => Math.min(...ln.slice(1).map((b, i) => segDist(p, ln[i], b)))));
  const leaders = leaderLayer.getLayers().filter((l) => l.getLatLngs).map((l) => l.getLatLngs());
  return state.map.hotspots.map((s) => ({ n: s.number, shown: !!s.marker, meters: metersToRoutes(s.lat, s.lon),
      truePx: near(px([s.lat, s.lon])), nudge: s.marker ? px([s.lat, s.lon]).distanceTo(px(s.marker.getLatLng())) : null,
      leader: leaders.some((ll) => ll[0].lat === s.lat && ll[0].lng === s.lon) }));
})()"""


@pytest.mark.parametrize("zoom", [None, 14, 16])
def test_every_hotspot_marker_sits_on_a_drawn_route(page, base_url, zoom):
    data = lga_map()
    data["hotspots"].append({**data["hotspots"][0], "number": 9, "lat": 40.7000, "lon": -73.8000})  # a stretch on no drawn route
    done = next(e for e in json.loads((app.HERE / "data" / "saved_examples.json").read_text())["columbia-union-sq"]["events"] if e["type"] == "done")
    body = "".join(f"data: {json.dumps(e)}\n\n" for e in [{"type": "start", "session_id": "t"}, {"type": "routes", "data": data}, {**done, "map": data}])
    page.goto(base_url)
    page.route("**/run?*", lambda r: r.fulfill(status=200, headers={"Content-Type": "text/event-stream"}, body=body))
    page.fill("#start", "Union Square, Manhattan")
    page.fill("#end", "LaGuardia Airport, Queens")
    page.click("#go")
    page.wait_for_selector(".spot-pin", timeout=15000)
    page.wait_for_timeout(1200)
    page.unroute("**/run?*")
    if zoom:
        page.evaluate(f"map.setView([40.7528, -73.922], {zoom}, {{ animate: false }})")
        page.wait_for_timeout(400)
    spots = page.evaluate(SPOT_CHECK)
    shown = [s for s in spots if s["shown"]]
    assert [s["n"] for s in spots if not s["shown"]] == [9]  # off every route: not drawn
    assert len(shown) == 5 and page.locator(".spot-pin").count() == 5
    for s in shown:
        assert s["meters"] < 5, s  # its true point is on a route's line
        assert s["truePx"] <= 6, s  # and on a line as drawn (shared streets shift up to 5 px)
        assert s["nudge"] <= 12.6, s  # the marker moved at most about 12 px
        assert s["leader"] or s["nudge"] <= 1, s  # with a leader line back to its true point when it moved
    # The recommended route is drawn last, on top of the others and of the selected route's risk coloring
    page.evaluate("selectRoute(distinctRoutes(state.map).find((r) => !isRecommended(r)).route_ref)")
    on_top = page.evaluate("routeLayer.getLayers().at(-1) === state.layers[distinctRoutes(state.map).find(isRecommended).route_ref]")
    assert on_top


def test_routes_on_the_same_street_are_drawn_side_by_side(page, base_url):
    page.goto(base_url + "/?example=columbia-union-sq")
    page.wait_for_selector("#summary .support")
    shifts = page.evaluate("""(() => { const routes = distinctRoutes(state.map), rec = routes.find(isRecommended);
        const offs = sharedOffsets(routes, rec); return routes.map((r) => [r === rec, [...new Set(offs.get(r))].sort()]); })()""")
    assert any(len(set(o)) > 1 for _, o in shifts)  # some streets are shared
    for is_rec, o in shifts:
        assert (o == [0]) if is_rec else all(abs(x) <= 5 for x in o)  # the recommended route stays on the true street


def test_a_session_syncs_restores_and_deletes(page, base_url, monkeypatch):
    from tests.test_sessions import DownStore, MemoryStore
    store = MemoryStore()
    monkeypatch.setattr(app, "session_store", store)
    app.session_asked.clear()
    page.goto(base_url)
    page.evaluate("localStorage.clear()")
    body = saved_stream()
    page.route("**/run?*", lambda r: r.fulfill(status=200, headers={"Content-Type": "text/event-stream"}, body=body))
    page.fill("#start", "Prospect Heights, Brooklyn")
    page.fill("#end", "DUMBO, Brooklyn")
    page.click("#go")
    page.wait_for_function("localStorage.getItem('aveHistory')", timeout=15000)
    page.unroute("**/run?*")
    sid = page.evaluate("sessionId()")
    for _ in range(50):  # the page saves to the session a moment after the trip finishes
        if sid in store.files:
            break
        page.wait_for_timeout(100)
    assert len(sid) == 22 and len(json.loads(store.files[sid])["trips"]) == 1
    # Another browser: nothing local, restore by pasting the ID
    page.evaluate("localStorage.clear()")
    page.goto(base_url + "/#history")
    page.wait_for_selector("#historyCard:not(.hidden)")
    assert page.inner_text("#sessionId") != sid and "Keep it private" in page.inner_text("#sessionBox")
    page.fill("#sessionRestore", "not-a-real-session-id!")
    page.click("#sessionRestoreGo")
    assert page.inner_text("#sessionStatus") == "No session found"
    page.fill("#sessionRestore", sid)
    page.click("#sessionRestoreGo")
    page.wait_for_selector(".hist-item")
    assert page.inner_text("#sessionId") == sid and "Restored 1 trip" in page.inner_text("#sessionStatus")
    calls = count_model_requests(page)
    page.locator(".hist-item").click()
    page.wait_for_selector("#summary .support")
    assert page.text_content("#savedTag") == "From your History" and calls == []
    # Delete: two taps, the server copy and local History both go
    page.click("#navHistory")
    page.click("#sessionDelete")
    page.click("#sessionDelete")
    page.wait_for_function("document.getElementById('sessionStatus').textContent.startsWith('Session deleted')")
    assert sid not in store.files and page.evaluate("localStorage.getItem('aveHistory')") is None
    assert page.inner_text("#sessionId") != sid
    # The bucket down: History still works here, and the page says sessions can't sync
    monkeypatch.setattr(app, "session_store", DownStore())
    page.evaluate("pushSession()")
    page.wait_for_function("document.getElementById('sessionStatus').textContent.includes(\"can't sync right now\")")
    page.evaluate("localStorage.clear()")


@pytest.mark.parametrize("width", [1440, 390])
def test_the_year_sparkline_is_padded_labeled_and_matches_the_hour_chart(page, base_url, width):
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(base_url + "/?example=columbia-union-sq")
    page.wait_for_selector(".timing-grid svg.years")
    out = page.evaluate("""(() => { const y = document.querySelector('svg.years'), h = document.querySelector('svg.hours');
        const box = y.getBoundingClientRect(), yr = [...y.querySelectorAll('g.yr')];
        const texts = yr.map((g) => [...g.querySelectorAll('text')].map((t) => t.textContent));
        const circles = yr.map((g) => g.querySelector('circle').getBoundingClientRect());
        const labels = [...y.querySelectorAll('text')].map((t) => t.getBoundingClientRect());
        const inside = labels.every((r) => r.left >= box.left - 0.5 && r.right <= box.right + 0.5 && r.top >= box.top - 0.5 && r.bottom <= box.bottom + 0.5);
        const overlap = labels.some((a, i) => labels.some((b, j) => j > i && a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom));
        const mid = (c) => (c.left + c.right) / 2, gaps = circles.slice(1).map((c, i) => Math.round(mid(c) - mid(circles[i])));
        const base = y.querySelector('line').getBoundingClientRect().top, hbase = h.querySelector('line').getBoundingClientRect().top - h.getBoundingClientRect().top;
        return { texts, inside, overlap, gaps, hollow: yr.at(-1).querySelector('circle').getAttribute('fill'), dashed: !!y.querySelector('polyline[stroke-dasharray]'),
                 heights: [Math.round(box.height), Math.round(h.getBoundingClientRect().height)], baselines: [Math.round(base - box.top), Math.round(hbase)],
                 tips: yr.map((g) => g.querySelector('title').textContent), belowDots: circles.every((c) => c.bottom < base) }; })()""")
    years = [t[1] for t in out["texts"]]
    assert years == ["21", "22", "23", "24", "25", "26*"]
    assert all(t[0].lstrip("~").isdigit() for t in out["texts"])  # a count above every point
    assert out["inside"] and not out["overlap"] and len(set(out["gaps"])) == 1  # padded, nothing touching, even spacing
    assert out["hollow"] == "#fff" and out["dashed"] and out["belowDots"]
    assert out["heights"][0] == out["heights"][1] and out["baselines"][0] == out["baselines"][1]  # a set with the hour chart
    assert "2026: " in out["tips"][-1] and "so far" in out["tips"][-1]  # every point has its number on hover
    page.set_viewport_size({"width": 1280, "height": 800})



def test_weight_switches_between_kg_and_lb_without_changing_calories(page, base_url):
    page.goto(base_url)
    page.evaluate("localStorage.removeItem('aveWeightKg'); localStorage.removeItem('aveWeightUnit')")
    assert page.evaluate("weightUnit()") == "lb"  # an en-US browser starts in pounds
    page.goto(base_url + "/?example=columbia-union-sq")
    page.wait_for_selector("#routeCards .weight-input")
    card = page.locator("#routeCards .card.route").first
    box, kcal = card.locator(".weight-input"), card.locator(".kcal")
    assert box.input_value() == "154" and card.locator(".unit-toggle [aria-pressed=true]").inner_text() == "lb"  # 70 kg
    before = kcal.inner_text()
    card.locator(".unit-toggle button", has_text="kg").click()
    assert box.input_value() == "70" and kcal.inner_text() == before  # the same weight, so the same calories
    assert page.evaluate("localStorage.getItem('aveWeightUnit')") == "kg"
    card.locator(".unit-toggle button", has_text="lb").click()
    box.fill("180")
    box.dispatch_event("change")
    assert abs(page.evaluate("weightKg()") - 180 / 2.2046) < 0.01  # kept in kg
    after_lb = kcal.inner_text()
    card.locator(".unit-toggle button", has_text="kg").click()
    assert box.input_value() == "82" and kcal.inner_text() == after_lb  # 180 lb = 81.6 kg, rounded
    assert page.evaluate("[shownWeight(100, 'lb'), shownWeight(100, 'kg'), Math.round(toKg(220.46, 'lb'))]") == [220, 100, 100]
    # A browser outside the US starts in kg, and storage that throws falls back cleanly
    assert page.evaluate("""(() => { const real = Object.getOwnPropertyDescriptor(Navigator.prototype, 'language');
        Object.defineProperty(navigator, 'language', { value: 'en-GB', configurable: true }); localStorage.removeItem('aveWeightUnit');
        const u = weightUnit(); delete navigator.language; return u; })()""") == "kg"
    page.evaluate("localStorage.removeItem('aveWeightKg'); localStorage.removeItem('aveWeightUnit')")


def ride(i, start, end, miles, minutes, streets, lanes, ridden=True):
    route = {"miles": miles, "minutes": minutes, "miles_by_lane": lanes, "streets": streets}
    return {"id": i, "at": 1_700_000_000_000 + i, "ridden": ridden, "trip": {"start": start, "end": end, "when": ""},
            "rec": {"name": "Balanced", "line": [[40.7, -73.99], [40.71, -73.98]], "miles": miles, "minutes": minutes},
            "result": {"map": {"recommended_route": "balanced", "routes": {"balanced": route}, "start": {"label": start}, "end": {"label": end}}}}


RIDES = [
    ride(1, "DUMBO, Brooklyn", "Union Square, Manhattan", 3.0, 20, [{"name": "Manhattan Bridge", "borough": "Brooklyn", "miles": 1.3}], {"protected": 2.0, "path": 0.5, "painted": 0.5}),
    ride(2, "Astoria, Queens", "Yankee Stadium, Bronx", 6.0, 40, [{"name": "Shore Boulevard", "borough": "Queens", "miles": 1}], {"protected": 1.0, "path": 4.0}),
    ride(3, "DUMBO, Brooklyn", "Union Square, Manhattan", 3.0, 20, [], {"path": 2.0, "none": 1.0}),
    ride(4, "St. George, Staten Island", "St. George, Staten Island", 2.1, 14, [], {"path": 2.1}),
    ride(5, "Harlem, Manhattan", "Inwood, Manhattan", 20.0, 120, [], {"none": 20.0}, ridden=False),  # not ridden: not counted
]


def test_ride_stats_count_only_trips_marked_ridden(page, base_url):
    page.goto(base_url)
    st = page.evaluate(f"rideStats({json.dumps(RIDES)}, 70)")
    assert st["rides"] == 4 and st["miles"] == 14.1 and st["minutes"] == 94
    assert st["safeMiles"] == 2.5 + 5.0 + 2.0 + 2.1
    assert st["kcal"] == sum(page.evaluate(f"kcal({m}, {t}, 70)") for m, t in [(3, 20), (6, 40), (3, 20), (2.1, 14)])
    assert st["longest"] == {"trip": "Astoria, Queens → Yankee Stadium, Bronx", "miles": 6}
    assert st["mostRidden"] == {"trip": "DUMBO, Brooklyn → Union Square, Manhattan", "times": 2}
    assert st["boroughs"] == ["Manhattan", "Brooklyn", "Queens", "Bronx", "Staten Island"]
    assert st["badges"] == {"allBoroughs": True, "bridgeCrosser": True, "greenwayRegular": True}  # rides 2, 3, 4 are half or more path
    assert page.evaluate(f"rideStats({json.dumps(RIDES[2:4])}, 70).badges") == {"allBoroughs": False, "bridgeCrosser": False, "greenwayRegular": False}


def test_ride_comparisons_use_the_fixed_distances(page, base_url):
    page.goto(base_url)
    fun = page.evaluate("comparisons(26.2)")
    assert fun == ["About 4.3 laps of the Central Park loop.", "About 2.0 times the length of Manhattan.",
                   "Like crossing the Brooklyn Bridge 24 times.", "About 1.0 NYC Marathons.",
                   "About 10 kg of CO2 not emitted, compared with driving the same miles."]
    assert page.evaluate("comparisons(13.1)")[3] == "About 50% of an NYC Marathon." and page.evaluate("comparisons(0)") == []
    assert not any(w in " ".join(fun).lower() for w in ["pizza", "bagel", "slice", "donut", "burger"])


def test_your_rides_card_in_history(page, base_url, monkeypatch):
    from tests.test_sessions import MemoryStore
    monkeypatch.setattr(app, "session_store", MemoryStore())
    page.goto(base_url)
    page.evaluate(f"localStorage.setItem('aveHistory', JSON.stringify({json.dumps([{**r, 'ridden': False} for r in RIDES])}))")
    page.reload()
    page.click("#navHistory")
    page.wait_for_selector("#historyCard:not(.hidden)")
    assert "Mark a trip as ridden to start your stats." in page.inner_text("#rideStats")
    page.locator(".hist-row").nth(1).locator(".rode input").check()
    page.wait_for_selector("#rideStats .stats-grid")
    text = page.inner_text("#rideStats")
    assert "Based on rides you marked as ridden." in text and "Longest ride" in text and "Boroughs: Queens, Bronx" in text
    assert page.locator("#rideStats .fun").inner_text().count("About") + page.locator("#rideStats .fun").inner_text().count("Like") == 2
    assert page.locator(".badge-tile.locked").count() == 11 and page.inner_text(".badge-count") == "1 of 12 badges"  # First ride
    assert page.evaluate("readHistory().find((x) => x.id === 2).ridden") is True  # kept in History, and so in the session
    page.evaluate("localStorage.clear()")


MAP_STATE = """({ ride: rideLayer.getLayers().length, rideName: document.querySelector('.ride.active')?.dataset.ride || null,
    loop: loopLayer.getLayers().length, routes: routeLayer.getLayers().length,
    detail: !document.getElementById('rideDetail').classList.contains('hidden'), loopCard: !document.getElementById('loopCard').classList.contains('hidden') })"""


def test_picking_another_ride_clears_the_last_one(page, base_url):
    page.goto(base_url + "/#popular")
    page.wait_for_selector(".ride")
    page.locator(".ride", has_text="Central Park loop").click()
    page.locator("#rideDetail button", has_text="Ride here").click()  # the loop is drawn with its card
    assert page.evaluate(MAP_STATE)["loop"] > 0
    page.locator(".ride", has_text="Hudson River Greenway").click()
    now = page.evaluate(MAP_STATE)
    assert now == {"ride": 1, "rideName": "Hudson River Greenway", "loop": 0, "routes": 0, "detail": True, "loopCard": False}
    hudson = page.evaluate("rideLayer.getLayers()[0].getLatLngs().length")
    assert hudson == page.evaluate("rides.find((r) => r.name === 'Hudson River Greenway').line[0].length")
    # A route result, then a ride: the routes go
    page.goto(base_url + "/?example=columbia-union-sq")
    page.wait_for_selector("#summary .support")
    page.locator(".ride", has_text="Hudson River Greenway").click()
    assert page.evaluate(MAP_STATE)["routes"] == 0 and not page.is_visible("#summary")


def test_a_loop_fills_its_start_and_never_shows_the_red_error(page, base_url):
    page.goto(base_url + "/#popular")
    page.wait_for_selector(".ride")
    runs = []
    page.route("**/run?*", lambda r: (runs.append(r.request.url), r.abort()))
    page.locator(".ride", has_text="Central Park loop").click()
    page.locator("#rideDetail button", has_text="Ride here").click()
    assert page.input_value("#start") == page.input_value("#end") == "Central Park loop start, Columbus Circle"
    assert "This is a loop, so there's nothing to compare. It starts and finishes here." in page.inner_text("#loopNote")
    page.click("#go")
    assert runs == [] and not page.is_visible("#errorBox") and page.is_visible("#loopNote")
    # Get me to the start: From clears, To stays the loop's start, and Compare routes runs as usual
    page.click("#loopToStart")
    assert page.input_value("#start") == "" and page.input_value("#end") == "Central Park loop start, Columbus Circle"
    page.click("#go")
    assert runs == [] and not page.is_visible("#errorBox")  # From still empty: the gray note, not the red box
    page.fill("#start", "Columbia University")
    page.click("#go")
    page.wait_for_timeout(300)
    assert len(runs) == 1 and "Central+Park+loop+start" in runs[0]
    page.unroute("**/run?*")


def test_the_loop_start_is_a_place_routing_knows():
    place = app.tools.geocode("Central Park loop start, Columbus Circle")
    assert (round(place["lat"], 4), round(place["lon"], 4)) == (40.7672, -73.9775)
    assert "routes" in app.tools.route_options("Columbia University", "Central Park loop start, Columbus Circle")


def test_compare_routes_is_clickable_after_ride_here_and_says_why_when_it_is_not(page, base_url):
    page.goto(base_url + "/#popular")
    page.wait_for_selector(".ride")
    page.locator(".ride", has_text="Hudson River Greenway").click()
    page.locator("#rideDetail button", has_text="Ride here").click()
    assert (page.input_value("#start"), page.input_value("#end")) == ("Battery Park", "George Washington Bridge")
    assert page.is_enabled("#go") and not page.is_visible("#formNote")
    page.evaluate("busy(true)")
    assert not page.is_enabled("#go") and "Comparing routes for this trip now" in page.inner_text("#formNote")
    page.locator(".ride", has_text="Central Park loop").click()  # picking a ride stops a running trip
    assert page.is_enabled("#go") and not page.is_visible("#formNote")


def test_the_empty_form_message_is_gray_not_red(page, base_url):
    page.goto(base_url)
    page.fill("#start", "")
    page.fill("#end", "")
    page.click("#go")
    assert page.inner_text("#formNote") == "Enter where you start and where you're going."
    assert not page.is_visible("#errorBox")
    color = page.evaluate("getComputedStyle(document.getElementById('formNote')).color")
    assert color == "rgb(75, 87, 103)"  # --muted


def test_badges_are_subway_tokens_with_progress_dates_and_a_pop(page, base_url, monkeypatch):
    from tests.test_sessions import MemoryStore
    monkeypatch.setattr(app, "session_store", MemoryStore())
    page.set_viewport_size({"width": 390, "height": 900})
    page.goto(base_url)
    page.evaluate(f"localStorage.setItem('aveHistory', JSON.stringify({json.dumps([{**r, 'ridden': False} for r in RIDES])}))")
    page.reload()
    page.click("#signMenu")
    page.click("#navHistory")
    page.wait_for_selector(".badge-tile")
    tiles = page.evaluate("""[...document.querySelectorAll('.badge-tile[data-badge="allBoroughs"], .badge-tile[data-badge="bridgeCrosser"], .badge-tile[data-badge="greenwayRegular"]')].map((t) => ({ key: t.dataset.badge, locked: t.classList.contains('locked'),
        color: getComputedStyle(t.querySelector('.bullet-mark')).backgroundColor, letter: t.querySelector('.bullet-mark').textContent,
        round: getComputedStyle(t.querySelector('.token')).borderRadius, size: t.querySelector('.token').getBoundingClientRect().width,
        lock: !!t.querySelector('.token-lock'), line: t.querySelector('.badge-line').textContent, tip: t.title, top: Math.round(t.getBoundingClientRect().top) }))""")
    assert [(t["letter"], t["color"]) for t in tiles] == [("5", "rgb(0, 147, 60)"), ("B", "rgb(255, 99, 25)"), ("G", "rgb(108, 190, 69)")]
    assert all(t["locked"] and t["lock"] and t["round"] == "50%" and t["size"] == 64 for t in tiles)
    assert [t["line"] for t in tiles] == ["0 of 5 boroughs", "0 of 1 crossing", "0 of 3 rides"] and tiles[0]["tip"] == "Ride in all five boroughs"
    rows = page.evaluate("[...document.querySelectorAll('.badge-tile')].map((t) => Math.round(t.getBoundingClientRect().top))")
    assert rows.count(rows[0]) == 3  # three to a row on a phone
    # Marking rides: badges earned by that tap pop, then show their date
    popped = []
    for i in range(4):
        page.locator(".hist-row").nth(i).locator(".rode input").check()
        popped += page.eval_on_selector_all(".token.pop", "ts => ts.map((t) => t.closest('.badge-tile').dataset.badge)")
    assert [b for b in popped if b in ("bridgeCrosser", "allBoroughs", "greenwayRegular")] == ["bridgeCrosser", "allBoroughs", "greenwayRegular"]
    assert popped[0] == "firstRide"
    for key in ["bridgeCrosser", "allBoroughs", "greenwayRegular"]:
        tile = page.locator(f'.badge-tile[data-badge="{key}"]')
        assert "locked" not in tile.get_attribute("class") and tile.locator(".token-lock").count() == 0
        assert tile.locator(".badge-line").inner_text().startswith("Earned ")
    page.locator(".badge-tile", has_text="Bridge crosser").click()
    assert "East River bridge" in page.inner_text("#badgeDetail") and "DUMBO, Brooklyn → Union Square" in page.inner_text("#badgeDetail")
    page.locator(".hist-row").nth(3).locator(".rode input").uncheck()  # Staten Island unmarked: back to 4 of 5, with a bar
    tile = page.locator(".badge-tile", has_text="All five boroughs")
    assert "locked" in tile.get_attribute("class") and tile.locator(".badge-line").inner_text() == "4 of 5 boroughs"
    assert tile.locator(".badge-bar span").get_attribute("style") == "width: 80%;"
    tile.click()
    assert "So far: 4 of 5 boroughs" in page.inner_text("#badgeDetail")
    page.evaluate("localStorage.clear()")
    page.set_viewport_size({"width": 1280, "height": 800})


def test_the_badge_pop_respects_reduced_motion(page, base_url):
    page.emulate_media(reduced_motion="reduce")
    page.goto(base_url)
    page.evaluate("document.body.insertAdjacentHTML('beforeend', '<div class=\"token pop\" id=\"tk\"></div>')")
    assert page.evaluate("getComputedStyle(document.getElementById('tk')).animationName") == "none"
    page.emulate_media(reduced_motion="no-preference")
    assert page.evaluate("getComputedStyle(document.getElementById('tk')).animationName") == "token-pop"



def trip_ridden(i, *, start="Union Square, Manhattan", end="Times Square, Manhattan", miles=2.0, when="", ref=None, at=None, lowest=False):
    """One History entry, marked ridden, for the badge rules."""
    route_ref = ref or f"route:{i:08x}"
    routes = {"balanced": {"route_ref": route_ref, "miles": miles, "minutes": miles * 6, "miles_by_lane": {"none": miles}, "streets": []},
              "lowest_risk": {"route_ref": route_ref if lowest else "route:ffffffff", "miles": miles, "minutes": miles * 6, "miles_by_lane": {}, "streets": []}}
    return {"id": i, "at": 1_700_000_000_000 + i, "riddenAt": at or 1_700_000_000_000 + i, "ridden": True,
            "trip": {"start": start, "end": end, "when": when},
            "rec": {"name": "Balanced", "line": [[40.7, -73.99]], "miles": miles, "minutes": miles * 6},
            "result": {"map": {"recommended_route": "balanced", "routes": routes, "start": {"label": start}, "end": {"label": end}}}}


def badge_info(page, rides):
    return page.evaluate(f"rideStats({json.dumps(rides)}, 70).badgeInfo")


def test_each_badge_rule(page, base_url):
    page.goto(base_url)
    one = badge_info(page, [trip_ridden(1)])
    assert one["firstRide"]["earned"] and not one["luckySeven"]["earned"] and one["luckySeven"]["have"] == 1
    assert badge_info(page, [trip_ridden(i) for i in range(7)])["luckySeven"]["earned"]
    # Ave A to B: a trip that starts or ends on Avenue A
    assert badge_info(page, [trip_ridden(1, start="199 Avenue A, Manhattan")])["aveAToB"]["earned"]
    assert badge_info(page, [trip_ridden(1, end="Tompkins Square, Ave A")])["aveAToB"]["earned"]
    assert not badge_info(page, [trip_ridden(1, start="Avenue B, Manhattan"), trip_ridden(2, start="200 Avenue Avenue, Queens")])["aveAToB"]["earned"]
    # Early bird: 3 rides with a morning ride time
    early = [trip_ridden(1, when="around 8am"), trip_ridden(2, when="around 6am"), trip_ridden(3, when="in the evening, around 6pm"), trip_ridden(4, when="")]
    assert badge_info(page, early)["earlyBird"]["have"] == 2
    assert badge_info(page, early + [trip_ridden(5, when="around 11:30am")])["earlyBird"]["earned"]
    assert not badge_info(page, [trip_ridden(i, when="at night, around 10pm") for i in range(3)])["earlyBird"]["earned"]
    # Long haul: one ride of 10 miles or more
    assert badge_info(page, [trip_ridden(1, miles=9.9), trip_ridden(2, miles=9.9)])["longHaul"] == {"have": 9.9, "need": 10, "earned": False}
    assert badge_info(page, [trip_ridden(1, miles=10.0)])["longHaul"]["earned"]
    # Marathon and Distance: miles in all
    m = badge_info(page, [trip_ridden(i, miles=5.0) for i in range(5)])
    assert m["marathon"]["have"] == 25 and not m["marathon"]["earned"]
    assert badge_info(page, [trip_ridden(i, miles=5.24) for i in range(5)])["marathon"]["earned"]
    d = badge_info(page, [trip_ridden(i, miles=9.9) for i in range(10)])
    assert d["distance"]["have"] == 99 and not d["distance"]["earned"]
    assert badge_info(page, [trip_ridden(i, miles=10.0) for i in range(10)])["distance"]["earned"]
    # Safety first: the recommended or lowest-risk route on 5 ridden trips
    assert badge_info(page, [trip_ridden(i) for i in range(4)])["safetyFirst"]["have"] == 4
    assert badge_info(page, [trip_ridden(i) for i in range(5)])["safetyFirst"]["earned"]
    fastest = trip_ridden(9)
    fastest["rec"]["route_ref"] = "route:0000fa57"  # a ride saved on another route doesn't count
    assert badge_info(page, [trip_ridden(i) for i in range(4)] + [fastest])["safetyFirst"]["have"] == 4
    # New streets: 5 different routes (the same route twice counts once)
    assert badge_info(page, [trip_ridden(i, ref="route:aaaaaaaa") for i in range(5)])["newStreets"]["have"] == 1
    assert badge_info(page, [trip_ridden(i) for i in range(5)])["newStreets"]["earned"]


def test_a_badge_date_is_the_ride_that_completed_it(page, base_url):
    page.goto(base_url)
    rides = [trip_ridden(1, miles=4, at=1_731_000_000_000), trip_ridden(2, miles=30, at=1_731_300_000_000), trip_ridden(3, miles=4, at=1_731_100_000_000)]
    info = badge_info(page, rides)
    assert info["marathon"]["at"] == 1_731_300_000_000 and info["firstRide"]["at"] == 1_731_000_000_000


def test_badges_sort_earned_first_then_closest_and_count(page, base_url, monkeypatch):
    from tests.test_sessions import MemoryStore
    monkeypatch.setattr(app, "session_store", MemoryStore())
    page.goto(base_url)
    rides = [trip_ridden(i, miles=5.0, when="around 8am") for i in range(4)]  # 20 mi, 4 rides, 4 morning, 4 routes
    page.evaluate(f"localStorage.setItem('aveHistory', JSON.stringify({json.dumps(rides)}))")
    page.reload()
    page.click("#navHistory")
    page.wait_for_selector(".badge-tile")
    order = page.eval_on_selector_all(".badge-tile", "ts => ts.map((t) => [t.dataset.badge, t.classList.contains('locked')])")
    earned = [k for k, locked in order if not locked]
    assert set(earned) == {"firstRide", "earlyBird"} and [locked for _, locked in order] == [False] * 2 + [True] * 10
    locked = [k for k, is_locked in order if is_locked]
    # closest first: Safety first and New streets 4 of 5 (80%), Marathon 20 of 26.2 (76%), then Lucky seven 4 of 7 ...
    assert locked[:3] == ["safetyFirst", "newStreets", "marathon"] and locked[3] == "luckySeven"
    assert page.inner_text(".badge-count") == "2 of 12 badges"
    assert page.locator('.badge-tile[data-badge="aveAToB"] .badge-line').inner_text() == "0 of 1 ride"
    tile = page.locator('.badge-tile[data-badge="marathon"]')
    assert tile.locator(".badge-line").inner_text() == "20 of 26.2 mi" and tile.get_attribute("title") == "Ride 26.2 miles in all"
    n = page.locator('.badge-tile[data-badge="newStreets"] .bullet-mark')
    assert n.evaluate("e => [getComputedStyle(e).backgroundColor, getComputedStyle(e).color]") == ["rgb(252, 204, 10)", "rgb(0, 0, 0)"]
    colors = page.evaluate("Object.fromEntries(BADGES.map((b) => [b.letter, b.color]))")
    assert colors == {"1": "#EE352E", "7": "#B933AD", "A": "#0039A6", "E": "#0039A6", "L": "#A7A9AC", "M": "#FF6319", "D": "#FF6319",
                      "S": "#808183", "N": "#FCCC0A", "5": "#00933C", "B": "#FF6319", "G": "#6CBE45"}
    page.evaluate("localStorage.clear()")


def test_header_explore_link_works_before_any_trip(page, base_url):
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(base_url)
    page.wait_for_function("exploreData !== null", timeout=30000)
    page.uncheck("#exploreOn")
    page.click("#navHistory")
    page.click("#navExplore")
    page.wait_for_timeout(300)
    assert errors == [] and page.is_checked("#exploreOn") and not page.url.endswith("#history")


def test_small_phones_no_sideways_scroll_and_time_labels_fit(page, base_url):
    for width in (320, 390):
        page = page.context.browser.new_page(viewport={"width": width, "height": 760}, is_mobile=True)
        page.goto(base_url)
        page.wait_for_timeout(500)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
        fits = page.evaluate("""(() => { const s = document.getElementById('when'), c = document.createElement('canvas').getContext('2d'), cs = getComputedStyle(s);
            c.font = cs.font; const text = Math.max(...[...s.options].map((o) => c.measureText(o.text).width));
            return s.getBoundingClientRect().width >= text + parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight); })()""")
        assert fits, width
        page.close()



def test_popular_ride_cards_never_clip_and_rows_match(page, base_url):
    for width in (320, 390, 768, 1440):
        tab = page.context.browser.new_page(viewport={"width": width, "height": 900}, is_mobile=width < 900)
        tab.goto(base_url)
        tab.wait_for_selector("#rideList .ride")
        cards = tab.evaluate("""[...document.querySelectorAll('#rideList .ride')].map((c) => {
            const r = c.getBoundingClientRect(), tag = c.querySelector('.tag').getBoundingClientRect(), svg = c.querySelector('svg').getBoundingClientRect(),
                  kids = [...c.children].map((k) => k.getBoundingClientRect());
            return {name: c.dataset.ride, top: Math.round(r.top), height: r.height, clipped: c.scrollHeight > c.clientHeight + 1,
                    inside: kids.every((k) => k.bottom <= r.bottom - 4 && k.right <= r.right + 0.5),
                    tagBelowTitle: tag.top >= c.querySelector('h3').getBoundingClientRect().bottom,
                    svgRight: svg.left > tag.left && svg.right <= r.right, svgCentered: Math.abs((svg.top + svg.bottom) / 2 - (r.top + r.bottom) / 2) <= 2};
        })""")
        assert len(cards) >= 8
        for c in cards:
            assert not c["clipped"] and c["inside"] and c["tagBelowTitle"], (width, c)
            assert c["svgRight"] and c["svgCentered"], (width, c)
        rows = {}
        for c in cards:
            rows.setdefault(c["top"], []).append(c["height"])
        assert all(max(h) - min(h) < 1 for h in rows.values()), (width, rows)
        tab.close()
