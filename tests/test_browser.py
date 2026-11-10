"""The page in a real browser: typing in From shows suggestions. Needs Playwright and Chrome (uv sync --group build)."""

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
    page.evaluate("localStorage.removeItem('aveWeightKg')")
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
