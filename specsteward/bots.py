"""The few Telegram and Discord bot API calls the phone wizard needs, over plain HTTPS.

The token is a credential: it is never printed, and any error text that could contain it
(a URL, say) has it masked before it is shown.
"""

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

HTTP_TIMEOUT = 40  # seconds; above the long-poll wait below
POLL_SECONDS = 25
TELEGRAM_TOKEN = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
DISCORD_TOKEN = re.compile(r"^[A-Za-z0-9_.-]{50,}$")
USER_AGENT = "DiscordBot (https://github.com/Dinusha-Ekanayake/specsteward, 1)"

# (url, JSON body or None, headers) -> (HTTP status, parsed JSON body)
Fetch = Callable[[str, dict[str, Any] | None, dict[str, str]], tuple[int, Any]]


class BotError(Exception):
    """A bot API call failed. ``conflict`` means another copy of the bot is polling."""

    def __init__(self, message: str, conflict: bool = False, unreachable: bool = False) -> None:
        super().__init__(message)
        self.conflict = conflict
        self.unreachable = unreachable  # a network failure: says nothing about the token


def _fetch(url: str, body: dict[str, Any] | None, headers: dict[str, str]) -> tuple[int, Any]:
    if not url.startswith("https://"):
        raise ValueError("refusing a non-https bot API url")
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:  # noqa: S310
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read() or b"null")
        except ValueError:
            return error.code, None


def masked(text: str, token: str) -> str:
    return text.replace(token, "***") if token else text


def valid_token(kind: str, token: str) -> bool:
    return bool((TELEGRAM_TOKEN if kind == "telegram" else DISCORD_TOKEN).match(token))


@dataclass(frozen=True)
class Sender:
    user_id: str
    name: str  # "@handle (First Last)" or what is known
    text: str


class Telegram:
    def __init__(self, token: str, fetch: Fetch = _fetch) -> None:
        self._token = token
        self._fetch = fetch

    def _call(self, method: str, **params: Any) -> Any:
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        try:
            status, body = self._fetch(url, params or None, {})
        except (OSError, ValueError) as error:
            message = f"could not reach Telegram: {masked(str(error), self._token)}"
            raise BotError(message, unreachable=True) from None
        if isinstance(body, dict) and body.get("ok"):
            return body.get("result")
        description = body.get("description", "") if isinstance(body, dict) else ""
        if status == 401:
            raise BotError("Telegram rejected the token (check you copied all of it)")
        if status == 409:
            raise BotError(
                "another copy of this bot is running (a Claude Code session with the "
                "telegram plugin on); close it, then run this again",
                conflict=True,
            )
        raise BotError(f"Telegram {method} failed: {masked(description, self._token) or status}")

    def username(self) -> str:
        me = self._call("getMe")
        return str(me.get("username", "")) if isinstance(me, dict) else ""

    def skip_backlog(self) -> int:
        """The offset after every message already waiting, so only new ones count."""
        self._call("deleteWebhook")  # a webhook would block polling
        updates = self._call("getUpdates", offset=-1, timeout=0) or []
        return int(updates[-1]["update_id"]) + 1 if updates else 0

    def next_private_message(self, offset: int) -> tuple[int, Sender | None]:
        """Wait up to POLL_SECONDS. Returns the new offset and the first private message."""
        updates = self._call(
            "getUpdates", offset=offset, timeout=POLL_SECONDS, allowed_updates=["message"]
        )
        for update in updates or []:
            offset = int(update["update_id"]) + 1
            message = update.get("message") or {}
            if (message.get("chat") or {}).get("type") != "private":
                continue
            person = message.get("from") or {}
            full = " ".join(p for p in (person.get("first_name"), person.get("last_name")) if p)
            handle = f"@{person['username']}" if person.get("username") else ""
            name = f"{handle} ({full})" if handle and full else handle or full or "unknown"
            return offset, Sender(str(person.get("id")), name, str(message.get("text", "")))
        return offset, None

    def acknowledge(self, offset: int) -> None:
        """Mark messages up to ``offset`` as read, so the plugin does not see them again."""
        self._call("getUpdates", offset=offset, timeout=0)

    def send(self, chat_id: str, text: str) -> None:
        self._call("sendMessage", chat_id=chat_id, text=text)


def discord_username(token: str, fetch: Fetch = _fetch) -> str:
    headers = {"Authorization": f"Bot {token}", "User-Agent": USER_AGENT}
    try:
        status, body = fetch("https://discord.com/api/v10/users/@me", None, headers)
    except (OSError, ValueError) as error:
        raise BotError(f"could not reach Discord: {masked(str(error), token)}") from None
    if status == 401:
        raise BotError("Discord rejected the token (check you copied all of it)")
    if status != 200 or not isinstance(body, dict):
        raise BotError(f"Discord answered {status}")
    return str(body.get("username", ""))
