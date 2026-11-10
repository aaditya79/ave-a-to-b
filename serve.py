"""The app's entry point (Procfile: uvicorn serve:app). It opens the port at once and loads the full app (app.py:
the street network, the crash tables, the agents) in a background thread, so a new instance answers right away.

While the app loads:
  - the home page is served with its normal header and form, plus a small "Starting up" note that goes away
    when the app is ready
  - the header's CSS and script, the icons, and the link preview image are served from disk
  - every other request (data, routes, the agents) waits until the app is ready, then goes through

It logs three times, so a slow start can be traced to the container or to Python:
  process started (seconds after the container's clock says the process began), port open, app ready.
"""
import asyncio
import os
import socket
import threading
import time
from pathlib import Path

HERE = Path(__file__).parent
STARTED = time.time()
STARTING_NOTE = "Starting up, about 30 seconds. You can fill in your trip now."

# Served from disk while the app loads, so the page draws with its header in place
FILES = {"/static/header.css": ("static/header.css", "text/css"), "/static/header.js": ("static/header.js", "text/javascript"),
         "/favicon.svg": ("favicon.svg", "image/svg+xml"), "/og.png": ("og.png", "image/png")}


def log(message: str) -> None:
    print(f"startup: {message}", flush=True)


def process_start() -> float:
    """When this process began, from the kernel's records (Linux), else when this module was imported."""
    try:
        ticks = int(Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19])
        uptime = float(Path("/proc/uptime").read_text().split()[0])
        return time.time() - uptime + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return STARTED


def load_app():
    import app
    return app.app


def starting_page() -> bytes:
    """The home page as app.py serves it, with the "Starting up" note above the form."""
    html = (HERE / "index.html").read_text().replace("<!--#header-->", (HERE / "static" / "header.html").read_text(), 1)
    note = (f'<p id="startingNote" role="status" style="margin: 0 0 12px; padding: 8px 12px; border-radius: 8px; '
            f'background: #fff0e8; color: #1b2a3d; font-size: 14px;">{STARTING_NOTE}</p>\n'
            '<script>fetch("/ready").then(() => document.getElementById("startingNote")?.remove());</script>\n')
    return html.replace('<section class="card" id="formCard">', note + '<section class="card" id="formCard">', 1).encode()


class Launcher:
    def __init__(self, loader=load_app):
        self.loader = loader
        self.app = None
        self.ready = threading.Event()
        self.started = False
        self.lock = threading.Lock()

    def start(self, port: int | None = None) -> None:
        with self.lock:
            if self.started:
                return
            self.started = True
        begun = process_start()
        log(f"process started {STARTED - begun:.1f}s before Python ran this file")
        threading.Thread(target=self._load, args=(begun,), daemon=True).start()
        if port:
            threading.Thread(target=self._watch_port, args=(port, begun), daemon=True).start()

    def _watch_port(self, port: int, begun: float) -> None:
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                log(f"port {port} open {time.time() - begun:.1f}s after the process started")
                return
            except OSError:
                time.sleep(0.05)

    def _load(self, begun: float) -> None:
        t = time.time()
        while True:
            try:
                self.app = self.loader()
                break
            except Exception as e:  # keep the port open and try again rather than leave the instance up but empty
                log(f"loading the app failed ({type(e).__name__}: {e}), trying again in 5 seconds")
                time.sleep(5)
        log(f"app ready {time.time() - begun:.1f}s after the process started (loading took {time.time() - t:.1f}s)")
        self.ready.set()

    async def wait(self) -> None:
        while not self.ready.is_set():
            await asyncio.sleep(0.1)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    self.start(int(os.getenv("PORT", "0")) or None)
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        self.start()
        path = scope.get("path", "")
        if scope["type"] == "http" and path == "/ready":
            await self.wait()
            return await respond(send, 200, b"ready", "text/plain")
        if self.ready.is_set():
            return await self.app(scope, receive, send)
        if scope["type"] == "http" and scope["method"] in ("GET", "HEAD"):
            if path == "/":
                return await respond(send, 200, starting_page(), "text/html; charset=utf-8")
            if path in FILES:
                name, kind = FILES[path]
                return await respond(send, 200, (HERE / name).read_bytes(), kind)
        await self.wait()
        await self.app(scope, receive, send)


async def respond(send, status: int, body: bytes, kind: str) -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", kind.encode()), (b"content-length", str(len(body)).encode()), (b"cache-control", b"no-cache")]})
    await send({"type": "http.response.body", "body": body})


app = Launcher()
