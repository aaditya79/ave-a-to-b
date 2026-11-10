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
