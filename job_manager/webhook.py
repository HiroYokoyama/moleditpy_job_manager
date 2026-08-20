"""A line in a chat room when a job ends: posts to Slack/Discord/Teams/etc via
their incoming-webhook JSON POST, so a finished job reaches a phone.

The URL decides the payload key -- Discord reads ``content`` and rejects a
body with fields it doesn't know, so both aren't sent. Posting runs on a
daemon thread; a dead webhook must not freeze MoleditPy.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

#: Long enough for a slow round trip, short enough that a dead endpoint does
#: not keep a thread for the rest of the session.
TIMEOUT_SECONDS = 10.0

SLACK = "slack"
DISCORD = "discord"
GENERIC = "generic"

_DISCORD_HOSTS = ("discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com")
_SLACK_HOSTS = ("hooks.slack.com", "slack.com")


def flavour(url: str) -> str:
    """Which service this URL belongs to, as far as the payload is concerned."""
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return GENERIC
    if host in _DISCORD_HOSTS or host.endswith(".discord.com"):
        return DISCORD
    if host in _SLACK_HOSTS or host.endswith(".slack.com"):
        return SLACK
    return GENERIC


def service_name(url: str) -> str:
    """What to call it in the interface."""
    return {SLACK: "Slack", DISCORD: "Discord"}.get(flavour(url), "a generic JSON webhook")


def is_supported(url: str) -> bool:
    """Whether this is a URL worth posting to at all.

    ``http`` and ``https`` only, and a host. A typed-in path or a ``file://``
    URL would otherwise be handed to ``urlopen``, which is willing to open one.
    """
    try:
        parts = urllib.parse.urlsplit((url or "").strip())
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def build_payload(url: str, title: str, message: str) -> dict:
    """The body for this service."""
    kind = flavour(url)
    if kind == DISCORD:
        return {"content": f"**{title}**\n{message}"}
    if kind == SLACK:
        return {"text": f"*{title}*\n{message}"}
    return {"text": f"{title}\n{message}"}


def post(url: str, title: str, message: str, timeout: float = TIMEOUT_SECONDS) -> bool:
    """Post one message and wait for the answer. Never raises."""
    url = (url or "").strip()
    if not is_supported(url):
        return False
    body = json.dumps(build_payload(url, title, message)).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "MoleditPy-JobManager"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return 200 <= int(getattr(response, "status", 200) or 200) < 300
    except urllib.error.HTTPError as exc:
        # Usually a webhook deleted in the workspace; the job itself is unaffected.
        logging.warning("Job Manager: the chat webhook answered %s", exc.code)
        return False
    except Exception:
        logging.debug("Job Manager: the chat webhook could not be reached", exc_info=True)
        return False


def post_async(url: str, title: str, message: str) -> Optional[threading.Thread]:
    """Post without waiting. Returns the thread, so a test can join it."""
    if not is_supported((url or "").strip()):
        return None
    thread = threading.Thread(
        target=post, args=(url, title, message), name="jobmanager-webhook", daemon=True
    )
    thread.start()
    return thread


__all__ = [
    "DISCORD",
    "GENERIC",
    "SLACK",
    "TIMEOUT_SECONDS",
    "build_payload",
    "flavour",
    "is_supported",
    "post",
    "post_async",
    "service_name",
]
