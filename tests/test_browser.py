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
        assert facts["links"] == ["Explore the city", "Popular rides", "How we calculate risk"]
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
