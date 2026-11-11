"""Bot API calls: token checks, polling for the user's message, and never leaking the token."""

from typing import Any

import pytest

from specsteward import bots

TOKEN = "123456789:" + "A" * 35


class FakeTelegram:
    """Answers Telegram bot API calls from a script of updates."""

    def __init__(self, updates: list[dict[str, Any]] | None = None, status: int = 200) -> None:
        self.updates = updates or []
        self.status = status
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def __call__(
        self, url: str, body: dict[str, Any] | None, headers: dict[str, str]
    ) -> tuple[int, Any]:
        method = url.rsplit("/", 1)[1]
        self.calls.append((method, body))
        if self.status != 200:
            return self.status, {"ok": False, "description": f"error with {url}"}
        if method == "getMe":
            return 200, {"ok": True, "result": {"username": "my_bot"}}
        if method == "getUpdates":
            offset = (body or {}).get("offset", 0)
            if offset == -1:
                return 200, {"ok": True, "result": self.updates[-1:]}
            return 200, {
                "ok": True,
                "result": [u for u in self.updates if u["update_id"] >= offset],
            }
        return 200, {"ok": True, "result": True}


def _message(
    update_id: int, user_id: int, chat_type: str = "private", text: str = "hi"
) -> dict[str, Any]:
    person = {"id": user_id, "first_name": "Ann", "username": "ann"}
    return {
        "update_id": update_id,
        "message": {"from": person, "chat": {"type": chat_type}, "text": text},
    }


def test_token_shapes() -> None:
    assert bots.valid_token("telegram", TOKEN)
    assert not bots.valid_token("telegram", "123:short")
    assert bots.valid_token("discord", "x" * 72)
    assert not bots.valid_token("discord", "nope")


def test_username_and_send() -> None:
    fake = FakeTelegram()
    api = bots.Telegram(TOKEN, fake)
    assert api.username() == "my_bot"
    api.send("42", "hello")
    assert ("sendMessage", {"chat_id": "42", "text": "hello"}) in fake.calls


def test_backlog_is_skipped_and_only_private_messages_count() -> None:
    fake = FakeTelegram([_message(5, 1), _message(6, 2, "group"), _message(7, 3)])
    api = bots.Telegram(TOKEN, fake)
    assert api.skip_backlog() == 8  # messages sent before the wizard asked do not count
    assert fake.calls[0][0] == "deleteWebhook"
    offset, sender = api.next_private_message(6)
    assert sender is not None and sender.user_id == "3" and offset == 8
    assert sender.name == "@ann (Ann)"
    assert api.next_private_message(8) == (8, None)


@pytest.mark.parametrize(
    ("status", "words", "conflict"),
    [(401, "rejected the token", False), (409, "another copy", True), (500, "failed", False)],
)
def test_errors_are_explained_and_never_show_the_token(
    status: int, words: str, conflict: bool
) -> None:
    with pytest.raises(bots.BotError) as caught:
        bots.Telegram(TOKEN, FakeTelegram(status=status)).username()
    assert words in str(caught.value) and caught.value.conflict is conflict
    assert TOKEN not in str(caught.value)


def test_network_errors_mask_the_token() -> None:
    def down(url: str, body: object, headers: object) -> tuple[int, Any]:
        raise OSError(f"cannot open {url}")

    with pytest.raises(bots.BotError, match="could not reach Telegram") as caught:
        bots.Telegram(TOKEN, down).username()
    assert TOKEN not in str(caught.value)
    with pytest.raises(bots.BotError, match="could not reach Discord") as caught:
        bots.discord_username(TOKEN, down)


def test_discord_username() -> None:
    seen: dict[str, str] = {}

    def fetch(url: str, body: object, headers: dict[str, str]) -> tuple[int, Any]:
        seen.update(headers)
        return 200, {"username": "dbot"}

    assert bots.discord_username("tok", fetch) == "dbot"
    assert seen["Authorization"] == "Bot tok" and "DiscordBot" in seen["User-Agent"]
    with pytest.raises(bots.BotError, match="rejected"):
        bots.discord_username("tok", lambda u, b, h: (401, None))
    with pytest.raises(bots.BotError, match="answered 500"):
        bots.discord_username("tok", lambda u, b, h: (500, None))


def test_real_fetch_reads_http_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import urllib.error

    def refuse(request: object, timeout: float) -> object:
        body = io.BytesIO(b'{"ok": false, "description": "nope"}')
        raise urllib.error.HTTPError("u", 400, "bad", {}, body)  # type: ignore[arg-type]

    monkeypatch.setattr(bots.urllib.request, "urlopen", refuse)
    assert bots._fetch("https://x", {"a": 1}, {}) == (400, {"ok": False, "description": "nope"})
