"""The Host Monitor, read-only, on a port a browser can open.

Binds 127.0.0.1 and nothing else. Reaching it from a phone or from a laptop
somewhere else is ``tailscale serve``'s job::

    tailscale serve --bg 8770

which puts the same page on ``https://<machine>.<tailnet>.ts.net/`` with
Tailscale's own identity in front of it. Keeping the exposure there rather
than here is the whole point: this process never has to bind a routable
address, never has to decide whose certificate to trust, and never has to
grow an authentication story of its own beyond the token below.

Read-only on purpose. There is no route that cancels, submits, downloads or
deletes anything -- a link that leaks costs the reader a look at what is
running, not the run itself.

No Qt. The GUI thread hands over a finished snapshot through :meth:`publish`
and this module only ever reads it, so nothing here touches a widget from
the HTTP thread -- which is what a request handler reaching into a Qt object
would otherwise do on every hit.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, quote, urlsplit

from . import PLUGIN_VERSION
from .api_core import new_token, write_private_file

#: Deliberately not the API's 8765: running both at once is ordinary, and
#: sharing a number would make whichever started second fall back to a random
#: port that the copied command no longer names.
DEFAULT_PORT = 8770

BIND_HOST = "127.0.0.1"

#: The cookie the tokenised URL leaves behind, so a reload -- or a phone
#: reopening the tab tomorrow -- does not need the token in the address again.
COOKIE_NAME = "jm_monitor"

#: Kept beside the job API's token and read back on every start.
#:
#: Its own file, not the API's token: that one grants full control, and a
#: read-only page is not a reason to paste a full-control secret into a phone's
#: browser history.
#:
#: Persisted rather than minted per session, which is what it used to be. The
#: link is meant to be saved on another device, and a token that changed every
#: time the Host Monitor window was closed made that bookmark dead by the next
#: morning -- while the window itself came back automatically, so nothing
#: looked wrong from this side.
WEB_TOKEN_FILENAME = "web_monitor_token"


def web_token_path(directory: str) -> str:
    return os.path.join(directory, WEB_TOKEN_FILENAME)


def read_web_token(directory: str) -> str:
    try:
        with open(web_token_path(directory), "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def ensure_web_token(directory: str, renew: bool = False) -> str:
    """The page's secret, generated once and kept.

    ``renew=True`` mints a new one, which is how a link that has got away from
    you is cut off -- every saved bookmark and cookie stops working at once.
    """
    existing = "" if renew else read_web_token(directory)
    if existing:
        return existing
    token = new_token(16)
    write_private_file(web_token_path(directory), token + "\n")
    return token


#: The page's tab icon: a server rack with its lights on.
#:
#: A molecule with a bar chart was tried first and read as generic analytics --
#: the chart carried no meaning this plugin owns, and the application's own
#: icon is already the molecule. A rack says "the machines your work is running
#: on", which is what this page is a view of. The bottom unit takes MoleditPy's
#: blue so it belongs to the same family.
#:
#: Drawn for 16 px first rather than scaled down to it: the indicator dots are
#: deliberately oversized for their slabs, because at tab size a realistic LED
#: is a single pale pixel and the icon collapses into three grey bars.
#:
#: SVG rather than a .ico, and inlined as a data: URI rather than served from a
#: route, so the icon needs no second request and no second thing to get wrong.
#: A light rounded plate behind it on purpose: the units are near-black, and on
#: a dark browser tab a transparent version would show only the blue one.
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="7" fill="#ffffff"/>'
    '<rect x="4.5" y="5.5" width="23" height="6.5" rx="2.2" fill="#151515"/>'
    '<rect x="4.5" y="13.5" width="23" height="6.5" rx="2.2" fill="#151515"/>'
    '<rect x="4.5" y="21.5" width="23" height="6.5" rx="2.2" fill="#3577f7"/>'
    '<circle cx="8.6" cy="8.75" r="2" fill="#00e676"/>'
    '<circle cx="8.6" cy="16.75" r="2" fill="#00e676"/>'
    '<circle cx="8.6" cy="24.75" r="2" fill="#ffffff"/>'
    "</svg>"
)

#: The same drawing as a PNG, because Safari renders no SVG favicon at all
#: -- and Safari on a phone is the browser this page is mostly opened in.
#: Chrome and Firefox take the SVG above and never fetch this one.
#:
#: Inlined rather than served from a route on purpose: every route here is
#: behind the token, and a browser fetching an icon -- an apple-touch-icon
#: especially -- need not send the cookie, so the icon would 401.
#:
#: Generated from FAVICON_SVG by `python -m tests.regenerate_icons`.
#: test_icon.py re-renders the SVG and compares, so the two cannot drift.
FAVICON_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAACXBIWXMAAA7EAAAOxAGVKw4bAAACVElEQVRYhe2X"
    "vWtTURjGf+fmiya1ioKCSIlVEqFU0MXRSLsYig7FLI7dHHRpISCCxKXg5B/gVAQJSBHkzsHNRVuLi1C4qV2kDWo+"
    "bkhq7uuQRG9yrkO56e3SZzvvgfP8znPuebkHXBKRCREpiMiGiNRldKr31iyIyAReEpE5ESmP0PR/KovInJd50JoF"
    "UNKNZBOY9Izm8LQNzBjA0hGY0/NcUiKyAVx1z1iWhWmaNBqNkTglEgmy2SzJZHJ4akOJSB1IuM0zmQy2bSPREL+v"
    "nCZsVVHVli+IeDxOqVQahqgbbnMA0zS75uMRfry+w8/VeSrvFuhMnfIFYNs2pmkOl8eN4Uo/9tatSTqXuqZyMkbz"
    "XtoXgHtttzSAvkLl6uDY+uUbwEvh4UIqlQIg8nmXE4/f05q/TOTTd8befPVtlk7rKSoREXfBcRzy+TzFYhHbtn2b"
    "QvcDzOVyrKysYBiDoWsAQUs7Agi0D+gJBNwH9FsQcB/QAY77wHEfCFqefWCnIpS+dGi2R2MyFoXMdIgLZ5Q2pyWw"
    "UxHuv2jRbEM0DOnzBtauUGv6C2osCq8exTQI7Rb0d56IwerDGC8fRFlbjpI8q9MfRM12d+1haQD92G9Oh7jYM52I"
    "KxZueJ7WgSG8ADwb/re9wcjLe45vAA/Vw8AWrp/S/q43tx2eFve5fS3EuuWw9kGP76CaOqcd45YSkQLwpF9xBJ6/"
    "3cf8ONpbkL0eYvluBGOQ4dlRPkzKwIyhlKoCiwGbAywqpWp/RxLs43TWE0n+Pc/XRaQ2QtNab03tef4Hs9wvRDFA"
    "shUAAAAASUVORK5CYII="
)

#: For "Add to Home Screen", which is what someone watching a long run from
#: a phone actually does with this. Without it iOS uses a page screenshot.
TOUCH_ICON_PNG_B64 = (
    "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"
    "0L2fAAAAAElFTkSuQmCC"
)

FAVICON_DATA_URI = "data:image/svg+xml," + quote(FAVICON_SVG, safe="")


def tailscale_command(port: int) -> str:
    """The command that puts this port on the tailnet."""
    return f"tailscale serve --bg {int(port)}"


def tailscale_available() -> bool:
    """Whether the CLI is on PATH, for wording the hint rather than gating it."""
    return shutil.which("tailscale") is not None


def _run_tailscale(*args: str, timeout: int = 20) -> "tuple[bool, str]":
    """Run the CLI and say plainly whether it worked.

    A list, never a shell string: the port is the only variable here and it is
    an int, but building a command line for a shell to re-split is how that
    stops being true later.
    """
    binary = shutil.which("tailscale")
    if binary is None:
        return False, "Tailscale was not found on PATH."
    try:
        done = subprocess.run(  # noqa: S603 - fixed binary, no shell, int argument
            [binary, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            # Closed, not inherited: `serve` asks for confirmation on some
            # paths, and with the output captured that question is invisible.
            # It then waited for an answer nobody could see it wanting, and
            # the only symptom was "Tailscale did not answer in time".
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return False, "Tailscale did not answer in time."
    except OSError as exc:
        return False, f"Could not run Tailscale: {exc}"
    if done.returncode == 0:
        return True, (done.stdout or "").strip()
    # Its own message is far better than anything invented here: it is what
    # says "not logged in", "needs HTTPS enabled for the tailnet", or that
    # serve wants elevation on this platform.
    return False, (done.stderr or done.stdout or "").strip() or "Tailscale refused."


def tailscale_dns_name() -> str:
    """This machine's name on the tailnet, or "" if it cannot be read.

    Worth asking for rather than printing a ``<machine>.<tailnet>`` placeholder:
    the whole point of the link is to be copied, and a placeholder has to be
    hand-edited on a phone before it works.
    """
    ok, output = _run_tailscale("status", "--json", timeout=10)
    if not ok or not output:
        return ""
    try:
        name = (json.loads(output).get("Self") or {}).get("DNSName") or ""
    except (ValueError, AttributeError):
        return ""
    return name.rstrip(".")


#: Where HTTPS is turned on. Serve cannot work without it, and the setting is
#: a tailnet-wide one that only an admin of that tailnet can change.
HTTPS_HELP_URL = "https://tailscale.com/kb/1153/enabling-https"


def tailnet_https_ready() -> "tuple[bool, str]":
    """Whether this tailnet can issue the certificate ``serve`` needs.

    Checked before running anything, because the failure it prevents is not a
    quick error: without HTTPS enabled, ``serve`` has no certificate to get and
    waits -- so the symptom was a twenty-second pause and "Tailscale did not
    answer in time", which says nothing about the one setting that fixes it.
    """
    ok, output = _run_tailscale("status", "--json", timeout=10)
    if not ok:
        return False, output
    try:
        status = json.loads(output)
    except ValueError:
        return True, ""  # Unreadable is not the same as "known to be off".
    if status.get("BackendState") != "Running":
        return False, f"Tailscale is not connected (state: {status.get('BackendState')})."
    if not status.get("CertDomains"):
        return False, (
            "HTTPS is not enabled for this tailnet, and Tailscale Serve needs it "
            "to get a certificate.\n\n"
            "Enable it once, in the admin console under DNS > HTTPS Certificates:\n"
            f"{HTTPS_HELP_URL}\n\n"
            "Serving on this machine works without it -- only the tailnet link does not."
        )
    return True, ""


def serve_on_tailnet(port: int) -> "tuple[bool, str]":
    """Publish ``port`` to the tailnet. Equivalent to :func:`tailscale_command`."""
    ready, why = tailnet_https_ready()
    if not ready:
        return False, why
    # Longer than the rest: the first serve on a machine provisions a
    # certificate, which is a round trip to Let's Encrypt rather than a local
    # call to the daemon.
    return _run_tailscale("serve", "--bg", str(int(port)), timeout=90)


def stop_serving_on_tailnet() -> "tuple[bool, str]":
    """Withdraw whatever this machine is serving.

    Offered beside the publish button on purpose: an exposure that is one
    click to switch on and a manual search to switch off is a trap.
    """
    return _run_tailscale("serve", "reset")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    #: Same reasoning as the job API's: rebinding a port another MoleditPy is
    #: still serving would quietly show that one's hosts under this one's URL.
    allow_reuse_address = False
    monitor: Any = None


class _Handler(BaseHTTPRequestHandler):
    server_version = "MoleditPyJobMonitor/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        # The default writes every hit to stderr, which is MoleditPy's console.
        logging.debug("Job Manager web monitor: " + fmt, *args)

    # --- helpers ------------------------------------------------------------

    def _send(self, code: int, body: bytes, content_type: str, cookie: str = "") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The page is a live view of a private machine; a proxy or a phone
        # keeping yesterday's copy of it would be worse than a slow reload.
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        # Nothing here loads a script, a font or an image from anywhere, so the
        # strictest policy that still renders the page is the correct one --
        # but connect-src has to be granted explicitly. It falls back to
        # default-src, so 'none' blocked the page's own poll of /api/status and
        # left it saying "reconnecting..." for ever, against a server that was
        # answering every request perfectly.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; connect-src 'self'; img-src 'self'; "
            "style-src 'unsafe-inline'; script-src 'unsafe-inline'",
        )
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            # A phone that locked its screen mid-response is not an error the
            # user can do anything about.
            logging.debug("Job Manager web monitor: the client went away")

    def _token_offered(self) -> str:
        query = parse_qs(urlsplit(self.path).query)
        supplied = query.get("token", [""])[0]
        if supplied:
            return supplied
        for chunk in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = chunk.strip().partition("=")
            if name == COOKIE_NAME:
                return value
        return ""

    def _authorised(self) -> bool:
        monitor = getattr(self.server, "monitor", None)
        expected = monitor.token if monitor is not None else ""
        # compare_digest, not ==: a plain comparison returns faster the sooner
        # it finds a wrong byte, which over enough tries is a way to read the
        # secret one character at a time.
        return bool(expected) and secrets.compare_digest(self._token_offered(), expected)

    # --- routes -------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - http.server's spelling
        path = urlsplit(self.path).path.rstrip("/") or "/"
        # Icons first, and without the token. They have to be real URLs rather
        # than data: URIs -- Safari ignores a data: favicon completely, and iOS
        # will not take one as an apple-touch-icon at all, which is how the
        # icon came to work in Chrome and nowhere else. A browser fetching an
        # icon need not send the cookie, so gating these would 401 them; there
        # is nothing to gate anyway, since the drawing is the same in every
        # install and already public in the repository.
        served = ICON_ROUTES.get(path)
        if served is not None:
            body, content_type = served()
            self._send(200, body, content_type)
            return
        if not self._authorised():
            self._send(
                401,
                b"Unauthorised. Open the link the Host Monitor gave you.",
                "text/plain; charset=utf-8",
            )
            return
        # Set on any authorised hit, so the token can leave the address bar
        # after the first load. Not Secure-flagged: tailscale serve terminates
        # TLS in front of us and forwards plain HTTP, so a Secure cookie would
        # be dropped for the http://127.0.0.1 case and never sent back.
        cookie = f"{COOKIE_NAME}={self.server.monitor.token}; Path=/; HttpOnly; SameSite=Strict"
        if path == "/api/status":
            body = json.dumps(self.server.monitor.snapshot(), default=str).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8", cookie)
            return
        if path == "/":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8", cookie)
            return
        self._send(404, b"No such page.", "text/plain; charset=utf-8")


def _icon_png() -> "tuple[bytes, str]":
    return base64.b64decode(FAVICON_PNG_B64), "image/png"


def _icon_touch() -> "tuple[bytes, str]":
    return base64.b64decode(TOUCH_ICON_PNG_B64), "image/png"


def _icon_svg() -> "tuple[bytes, str]":
    return FAVICON_SVG.encode("utf-8"), "image/svg+xml"


#: Every path an icon is asked for, including the two iOS probes for at the
#: site root whether or not the page names them.
ICON_ROUTES = {
    "/icon.svg": _icon_svg,
    "/icon.png": _icon_png,
    "/favicon.ico": _icon_png,
    "/apple-touch-icon.png": _icon_touch,
    "/apple-touch-icon-precomposed.png": _icon_touch,
}


class WebMonitorServer:
    """Owns the socket and the last snapshot the GUI thread published."""

    def __init__(self, token: str = "") -> None:
        self._token = token or new_token(16)
        self._server: Optional[_Server] = None
        self._thread: Optional[threading.Thread] = None
        self._port = 0
        self._lock = threading.Lock()
        self._snapshot: Dict[str, Any] = {"hosts": [], "jobs": [], "generated": ""}

    # --- state --------------------------------------------------------------

    @property
    def token(self) -> str:
        return self._token

    @property
    def port(self) -> int:
        return self._port

    @property
    def running(self) -> bool:
        return self._server is not None

    def url(self, host: str = BIND_HOST) -> str:
        """The address to paste, token included so the first load authorises."""
        if not self._port:
            return ""
        return f"http://{host}:{self._port}/?token={self._token}"

    def tailscale_url(self, machine: str = "") -> str:
        """What the same page looks like once ``tailscale serve`` is on."""
        name = machine or "<machine>.<tailnet>.ts.net"
        return f"https://{name}/?token={self._token}"

    # --- data ---------------------------------------------------------------

    def set_token(self, token: str) -> None:
        """Replace the secret on a running server.

        Every saved link and every cookie stops working the moment this
        returns, which is the entire point of offering it.
        """
        self._token = str(token)

    def publish(self, snapshot: Dict[str, Any]) -> None:
        """Replace the served snapshot. Called from the GUI thread."""
        with self._lock:
            self._snapshot = snapshot

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            snapshot = dict(self._snapshot)
        # Stamped here rather than by the publisher: it is constant for the
        # life of the process, and filling it in at the point of service means
        # the number shown is always the code that answered.
        snapshot["version"] = PLUGIN_VERSION
        return snapshot

    # --- lifetime -----------------------------------------------------------

    def start(self, port: int = DEFAULT_PORT) -> int:
        if self.running:
            return self._port
        server = self._bind(int(port or 0))
        server.monitor = self
        self._server = server
        self._port = server.server_address[1]
        self._thread = threading.Thread(
            # Not the 0.5 s default: this is how long stop() blocks the GUI
            # thread when the window closes.
            target=lambda: server.serve_forever(poll_interval=0.05),
            name="job-manager-web-monitor",
            daemon=True,
        )
        self._thread.start()
        logging.info("Job Manager: web monitor listening on http://%s:%s", BIND_HOST, self._port)
        return self._port

    @staticmethod
    def _bind(port: int) -> _Server:
        try:
            return _Server((BIND_HOST, port), _Handler)
        except OSError as exc:
            if not port:
                raise
            # A taken port is not worth refusing to start over: the dialog
            # shows whichever number was bound, and the copied command takes
            # it from there rather than from the constant.
            logging.warning(
                "Job Manager: web monitor port %s is not available (%s); taking a free one",
                port,
                exc,
            )
            return _Server((BIND_HOST, 0), _Handler)

    def stop(self) -> None:
        server, thread = self._server, self._thread
        self._server = self._thread = None
        self._port = 0
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=2.0)


#: One file, no external anything: the CSP above forbids fetching a script or a
#: stylesheet, and a monitor that needs the internet to render would be useless
#: on exactly the flaky connection someone checks it from.
PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job Manager - Host Monitor</title>
<link rel="icon" type="image/svg+xml" href="icon.svg">
<link rel="icon" type="image/png" sizes="32x32" href="icon.png">
<link rel="apple-touch-icon" href="apple-touch-icon.png">
<style>
  /* Light is the default and dark is the override, so a browser that does not
     report a preference at all gets a readable page rather than a dark one on
     a white phone. The bar colours are picked per scheme, not shared: the dark
     set is muted so it does not glare, and those same muted tones on white are
     too pale to read a warning from across a desk. */
  :root { color-scheme: light;
          --bg:#f6f8fa; --card:#ffffff; --line:#d0d7de;
          --text:#1f2328; --dim:#59636e; --track:#eaeef2;
          --ok:#1a7f37; --warn:#9a6700; --bad:#cf222e; }
  /* One class, the way the Plugin Explorer page does it: the system
     preference only decides the starting state, and after that the button
     is the whole answer. A media query as well would fight the class on a
     device whose OS disagrees with what the user just pressed. */
  :root.dark {
    color-scheme: dark;
    --bg:#14171c; --card:#1d2128; --line:#2c313a;
    --text:#e6e9ef; --dim:#9aa3b2; --track:#0f1216;
    --ok:#4ac47a; --warn:#e0b341; --bad:#e05b4b; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  header { padding:14px 16px; border-bottom:1px solid var(--line);
           display:flex; align-items:center; gap:8px 12px; flex-wrap:wrap; }
  /* Its own row, always, at every width. Letting it sit beside the title
     when there happened to be room meant the controls jumped between the
     first line and the second as the clock text changed length -- on a phone,
     under the thumb already reaching for them. A row that is always there
     costs one line and never moves. */
  .controls { display:flex; align-items:center; gap:8px;
              flex:0 0 100%; flex-wrap:nowrap; white-space:nowrap; }
  h1 { font-size:16px; margin:0; font-weight:600; }
  #age { color:var(--dim); font-size:12px; flex:1 1 auto; min-width:0;
         overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  main { padding:16px; display:grid; gap:12px;
         grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 14px; }
  .name { font-weight:600; margin-bottom:2px; }
  .sub { color:var(--dim); font-size:12px; margin-bottom:10px; word-break:break-word; }
  .meter { margin:8px 0; }
  .meter .label { display:flex; justify-content:space-between; font-size:12px; color:var(--dim); }
  .bar { height:7px; background:var(--track); border-radius:4px; overflow:hidden; margin-top:3px; }
  .fill { height:100%; background:var(--ok); transition:width .3s; }
  .fill.warn { background:var(--warn); } .fill.bad { background:var(--bad); }
  .err { color:var(--bad); font-size:12px; }
  table { width:100%; border-collapse:collapse; margin-top:10px; font-size:12px; }
  td { padding:3px 0; vertical-align:top; }
  td.state { color:var(--dim); text-align:right; white-space:nowrap; padding-left:8px; }
  .none { color:var(--dim); font-size:12px; }
  footer { padding:0 16px 20px; color:var(--dim); font-size:12px; }
  h1 { flex:0 0 auto; }
  header label { color:var(--dim); font-size:12px; }
  select, button { background:var(--card); color:var(--text);
           border:1px solid var(--line); border-radius:6px; padding:3px 8px;
           font-size:12px; font-family:inherit; cursor:pointer; }
  #theme { display:inline-flex; align-items:center; justify-content:center; padding:4px; }
  :root.dark #theme .moon { display:none; }
  :root:not(.dark) #theme .sun { display:none; }
</style>
</head>
<body>
<header><h1>Host Monitor</h1><span id="age">connecting...</span>
<div class="controls">
<button id="theme" type="button" aria-label="Toggle theme" title="Light or dark">
  <svg class="sun" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
       stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <circle cx="12" cy="12" r="5"></circle><line x1="12" y1="1" x2="12" y2="3"></line>
    <line x1="12" y1="21" x2="12" y2="23"></line><line x1="4.2" y1="4.2" x2="5.6" y2="5.6"></line>
    <line x1="18.4" y1="18.4" x2="19.8" y2="19.8"></line><line x1="1" y1="12" x2="3" y2="12"></line>
    <line x1="21" y1="12" x2="23" y2="12"></line><line x1="4.2" y1="19.8" x2="5.6" y2="18.4"></line>
    <line x1="18.4" y1="5.6" x2="19.8" y2="4.2"></line></svg>
  <svg class="moon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
       stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path></svg>
</button>
<label for="every">Refresh</label>
<select id="every">
  <option value="2">2 s</option>
  <option value="4">4 s</option>
  <option value="10">10 s</option>
  <option value="30">30 s</option>
  <option value="60">1 min</option>
  <option value="300">5 min</option>
  <option value="0">Paused</option>
</select>
</div>
</header>
<main id="cards"></main>
<footer id="foot"></footer>
<script>
const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

function meter(label, fraction, detail) {
  const pct = Math.round(Math.max(0, Math.min(1, fraction || 0)) * 100);
  const cls = pct >= 90 ? "bad" : pct >= 70 ? "warn" : "";
  return `<div class="meter"><div class="label"><span>${esc(label)}</span>
    <span>${esc(detail)}</span></div>
    <div class="bar"><div class="fill ${cls}" style="width:${pct}%"></div></div></div>`;
}

function card(h) {
  const jobs = (h.jobs || []).map(j =>
    `<tr><td>${esc(j.name)}</td><td class="state">${esc(j.state)}</td></tr>`).join("");
  return `<div class="card">
    <div class="name">${esc(h.name)}</div>
    <div class="sub">${esc(h.summary || "")}</div>
    ${h.error ? `<div class="err">${esc(h.error)}</div>`
      : meter("CPU", h.load_fraction, h.load_detail || "") +
        meter("Memory", h.memory_fraction, h.memory_detail || "")}
    ${jobs ? `<table>${jobs}</table>` : `<div class="none">No active jobs</div>`}
  </div>`;
}

async function tick() {
  try {
    const r = await fetch("api/status", { credentials: "same-origin" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const d = await r.json();
    const hosts = d.hosts || [];
    document.getElementById("cards").innerHTML =
      hosts.length ? hosts.map(card).join("")
                   : `<div class="none">No hosts are being monitored.</div>`;
    document.getElementById("age").textContent = "updated " + (d.generated || "");
    document.getElementById("foot").textContent =
      "MoleditPy Job Manager version " + (d.version || "?")
      + " · " + hosts.length + " host(s)";
  } catch (e) {
    // Kept on screen rather than blanked: the last good reading is still the
    // most useful thing here while a phone reconnects.
    document.getElementById("age").textContent = "reconnecting...";
  }
}
// The interval is the page's alone: it is what a phone on a metered
// connection pays, and the person holding it is the only one who knows
// whether this tab is being watched or left open all afternoon. Kept in
// localStorage so a reload does not silently put it back to four seconds.
// The system preference decides where this starts and nothing more: after
// the first press the stored choice is the answer, on every device and
// whatever the OS later switches to. Same shape as the Plugin Explorer page.
const themeButton = document.getElementById("theme");
const root = document.documentElement;

function setTheme(dark) {
  root.classList.toggle("dark", dark);
  try { localStorage.setItem("jm_theme", dark ? "dark" : "light"); } catch (e) {}
}

let saved = null;
try { saved = localStorage.getItem("jm_theme"); } catch (e) {}
const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
root.classList.toggle("dark", saved === "dark" || (!saved && prefersDark));

themeButton.addEventListener("click", () => setTheme(!root.classList.contains("dark")));

const every = document.getElementById("every");
let timer = null;

function schedule() {
  if (timer !== null) { clearInterval(timer); timer = null; }
  const seconds = Number(every.value);
  try { localStorage.setItem("jm_every", String(seconds)); } catch (e) {}
  if (seconds > 0) { timer = setInterval(tick, seconds * 1000); }
  else { document.getElementById("age").textContent = "paused"; }
}

try {
  const saved = localStorage.getItem("jm_every");
  // Only if the saved value is still one this page offers: a stored 4 that no
  // longer appears in the list would leave the box blank and the timer unset.
  if (saved !== null && [...every.options].some(o => o.value === saved)) {
    every.value = saved;
  }
} catch (e) {}

every.addEventListener("change", () => { schedule(); if (Number(every.value) > 0) tick(); });
tick();
schedule();
</script>
</body>
</html>
"""

# Substituted once at import: the page is full of literal "%" and of braces,
# so neither %-formatting nor an f-string can be used on it.


__all__ = [
    "COOKIE_NAME",
    "DEFAULT_PORT",
    "ICON_ROUTES",
    "FAVICON_PNG_B64",
    "TOUCH_ICON_PNG_B64",
    "FAVICON_SVG",
    "PAGE",
    "WebMonitorServer",
    "ensure_web_token",
    "read_web_token",
    "web_token_path",
    "serve_on_tailnet",
    "stop_serving_on_tailnet",
    "tailnet_https_ready",
    "tailscale_available",
    "tailscale_command",
    "tailscale_dns_name",
]
