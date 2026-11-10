"""serve.py, the entry point: the port opens at once, the home page shows a "Starting up" note while the app
loads, and data requests wait for the app and then go through. The app's loading is slowed down here; no model
is called."""
import asyncio
import socket
import threading
import time

import httpx
import pytest
import uvicorn

import app
import serve


def slow_loader(seconds: float):
    def load():
        time.sleep(seconds)
        return app.app
    return load


def test_while_loading_the_page_and_its_files_answer_at_once_and_data_waits():
    launcher = serve.Launcher(slow_loader(1.0))

    async def scenario():
        transport = httpx.ASGITransport(app=launcher)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            t = time.monotonic()
            home = await c.get("/")
            css = await c.get("/static/header.css")
            fast = time.monotonic() - t
            stats = await c.get("/stats")  # waits for the app
            waited = time.monotonic() - t
            again = await c.get("/")
            return home, css, fast, stats, waited, again
    home, css, fast, stats, waited, again = asyncio.run(scenario())
    assert fast < 0.5 and home.status_code == 200 and css.status_code == 200
    assert serve.STARTING_NOTE in home.text and 'class="signbar"' in home.text and 'id="trip"' in home.text
    assert stats.status_code == 200 and "segments" in stats.text and waited >= 0.9
    assert again.status_code == 200 and serve.STARTING_NOTE not in again.text  # the app's own page once it's ready


def test_the_startup_times_are_logged(capsys):
    launcher = serve.Launcher(slow_loader(0.2))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(launcher, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    launcher.ready.wait(10)
    time.sleep(0.3)
    server.should_exit = True
    thread.join(timeout=5)
    out = capsys.readouterr().out
    assert "startup: process started" in out and "startup: app ready" in out and "loading took" in out


def test_procfile_starts_the_launcher():
    assert "uvicorn serve:app" in (serve.HERE / "Procfile").read_text()


sync_api = pytest.importorskip("playwright.sync_api")


def test_the_page_shows_the_note_then_works_once_the_app_is_ready():
    launcher = serve.Launcher(slow_loader(3.0))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(launcher, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    try:
        with sync_api.sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="chrome")
            except Exception:
                browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 390, "height": 844})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded", timeout=5000)
            assert page.is_visible("#startingNote") and page.is_visible("#signbar") and page.is_visible("#start")
            page.wait_for_function("exploreData !== null", timeout=30000)  # /explore waited, then went through
            page.wait_for_function("document.getElementById('statCrashes').textContent !== '…'", timeout=10000)
            page.wait_for_selector("#startingNote", state="detached", timeout=5000)  # /ready and /explore finish within a poll of each other
            assert errors == []
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
