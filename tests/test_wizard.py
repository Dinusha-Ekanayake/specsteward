"""The guided `sdd phone setup`: token, pairing on the user's own keyboard, lock, test message."""

import itertools
import json
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from specsteward import bots, phone, secret_store, wizard
from specsteward.__main__ import main
from tests.test_bots import TOKEN, FakeTelegram, _message

TELEGRAM = phone.channel_named("telegram")
DISCORD = phone.channel_named("discord")
CODE = "c0de42"


@pytest.fixture(autouse=True)
def pairing_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wizard, "_pairing_code", lambda: CODE)


class Script(wizard.Console):
    """Answers questions from a list and records what was said."""

    def __init__(self, secrets: Sequence[str] = (), answers: Sequence[bool] = ()) -> None:
        self.secrets = list(secrets)
        self.answers = list(answers)
        self.out: list[str] = []
        self.asked: list[str] = []

    def say(self, text: str = "") -> None:
        self.out.append(text)

    def secret(self, prompt: str) -> str:
        return self.secrets.pop(0)

    def yes(self, question: str, default: bool) -> bool:
        self.asked.append(question)
        return self.answers.pop(0) if self.answers else default

    @property
    def text(self) -> str:
        return "\n".join(self.out)


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for channel in (TELEGRAM, DISCORD):
        monkeypatch.setenv(channel.dir_var, str(tmp_path / channel.name))
    monkeypatch.setattr(phone, "setup", lambda channel: (True, ["ok   setup done"]))
    monkeypatch.setattr(wizard, "running_servers", lambda: {})
    monkeypatch.setattr(secret_store, "backend", lambda: "file")  # see FakeStore for the rest
    return tmp_path


class FakeStore:
    """An OS credential store that works, standing in for the real one."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.saved: dict[str, str] = {}
        monkeypatch.setattr(secret_store, "store", self.store)
        monkeypatch.setattr(secret_store, "load", lambda name: self.saved.get(name, ""))

    def store(self, name: str, token: str) -> str:
        self.saved[name] = token
        return "keychain"


def _run(console: Script, fake: FakeTelegram, clock: Iterator[float] | None = None) -> int:
    ticks = clock or itertools.count()
    return wizard.run(
        TELEGRAM,
        console,
        telegram=lambda token: bots.Telegram(token, fake),
        launch=lambda: 7,
        clock=lambda: next(ticks),
    )


def test_full_telegram_setup(state: Path) -> None:
    fake = FakeTelegram([_message(1, 99)])  # waiting before the wizard: skipped
    real = _message(2, 42, text=CODE)

    def poll(url: str, body: dict[str, Any] | None, headers: dict[str, str]) -> tuple[int, Any]:
        if url.endswith("getUpdates") and (body or {}).get("timeout"):
            fake.updates.append(real)
        return FakeTelegram.__call__(fake, url, body, headers)

    console = Script(secrets=["not a token", TOKEN], answers=[True])
    code = wizard.run(
        TELEGRAM,
        console,
        telegram=lambda token: bots.Telegram(token, poll),
        launch=lambda: 7,
        clock=itertools.count().__next__,
    )
    assert code == 7, console.text  # it started claude-phone
    env = (state / "telegram" / ".env").read_text(encoding="utf-8")
    assert env == f"TELEGRAM_BOT_TOKEN={TOKEN}\n"
    access = json.loads((state / "telegram" / "access.json").read_text(encoding="utf-8"))
    assert access == {"dmPolicy": "allowlist", "allowFrom": ["42"], "pending": {}, "groups": {}}
    assert "does not look like" in console.text and "@my_bot" in console.text
    assert "account 42" in console.text and CODE in console.text
    assert any(m == "sendMessage" for m, _ in fake.calls)
    assert TOKEN not in console.text  # never shown


def test_someone_else_is_ignored_until_you_write(state: Path) -> None:
    fake = FakeTelegram([_message(1, 5), _message(2, 42, text=CODE)])
    fake_offset_start = FakeTelegram.__call__

    def fetch(url: str, body: dict[str, Any] | None, headers: dict[str, str]) -> tuple[int, Any]:
        if url.endswith("getUpdates") and (body or {}).get("offset") == -1:
            return 200, {"ok": True, "result": []}  # nothing waiting before
        return fake_offset_start(fake, url, body, headers)

    console = Script(secrets=[TOKEN], answers=[False])
    code = wizard.run(
        TELEGRAM,
        console,
        telegram=lambda token: bots.Telegram(token, fetch),
        launch=lambda: 0,
        clock=itertools.count().__next__,
    )
    assert code == 0 and "Ignored" in console.text
    access = json.loads((state / "telegram" / "access.json").read_text(encoding="utf-8"))
    assert access["allowFrom"] == ["42"]
    assert "claude-phone" in console.out[-1]  # declined to start it now


def test_no_message_times_out(state: Path) -> None:
    console = Script(secrets=[TOKEN])
    code = _run(console, FakeTelegram(), iter([0, 0, 500, 500]))
    assert code == 1 and "no message arrived" in console.text
    assert not (state / "telegram" / "access.json").exists()


def test_an_existing_bot_and_lock_are_kept(state: Path) -> None:
    wizard.write_token(state / "telegram" / ".env", "TELEGRAM_BOT_TOKEN", TOKEN)
    wizard.write_lock(state / "telegram" / "access.json", "42")
    console = Script(answers=[True, True, False])
    fake = FakeTelegram()
    assert _run(console, fake) == 0
    assert "@my_bot" in console.asked[0] and "locked to account 42" in console.asked[1]
    assert not any(m == "getUpdates" for m, _ in fake.calls)  # no new pairing needed


def test_bad_tokens_give_up_after_three_tries(state: Path) -> None:
    console = Script(secrets=[TOKEN, TOKEN, TOKEN])
    assert _run(console, FakeTelegram(status=401)) == 1
    assert console.text.count("rejected the token") == 3 and "no working token" in console.text


def test_a_running_bot_copy_is_reported(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wizard, "running_servers", lambda: {"telegram": 2})
    wizard.write_token(state / "telegram" / ".env", "TELEGRAM_BOT_TOKEN", TOKEN)
    console = Script(answers=[True])  # use the saved bot, then polling hits the other copy
    fake = FakeTelegram()
    original = FakeTelegram.__call__

    def busy(url: str, body: dict[str, Any] | None, headers: dict[str, str]) -> tuple[int, Any]:
        if url.endswith("deleteWebhook"):
            return 409, {"ok": False, "description": "Conflict"}
        return original(fake, url, body, headers)

    code = wizard.run(
        TELEGRAM, console, telegram=lambda t: bots.Telegram(t, busy), launch=lambda: 0
    )
    assert code == 1
    assert "2 copy(ies)" in console.text and "another copy of this bot" in console.text


def test_setup_failure_stops_early(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(phone, "setup", lambda channel: (False, ["FAIL Bun: no unzip"]))
    console = Script()
    assert _run(console, FakeTelegram()) == 1 and console.out == ["FAIL Bun: no unzip"]


def test_discord_checks_the_token_and_explains_pairing(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bots, "discord_username", lambda token: "dbot")
    console = Script(secrets=["d" * 72], answers=[False])
    assert wizard.run(DISCORD, console, launch=lambda: 0) == 0
    assert read(state / "discord" / ".env") == "DISCORD_BOT_TOKEN=" + "d" * 72 + "\n"
    assert "/discord:access pair" in console.text and "Message Content Intent" in console.text


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_token_file_keeps_other_lines(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("OTHER=1\nTELEGRAM_BOT_TOKEN=old\n", encoding="utf-8")
    wizard.write_token(env, "TELEGRAM_BOT_TOKEN", "new")
    assert read(env) == "OTHER=1\nTELEGRAM_BOT_TOKEN=new\n"
    assert wizard.read_token(env, "TELEGRAM_BOT_TOKEN") == "new"
    assert wizard.read_token(tmp_path / "missing", "X") == ""


def test_lock_replaces_other_accounts_and_keeps_groups(tmp_path: Path) -> None:
    access = tmp_path / "access.json"
    access.write_text(json.dumps({"allowFrom": ["1", "2"], "groups": {"g": {}}}), "utf-8")
    assert wizard.write_lock(access, "2") == 1
    data = json.loads(read(access))
    assert data["allowFrom"] == ["2"] and data["groups"] == {"g": {}}
    assert wizard.locked_account(access) == "2"
    access.write_text("not json", encoding="utf-8")
    assert wizard.locked_account(access) == ""


def test_console_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter(["", "y", "no"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    console = wizard.Console()
    assert console.yes("q", True) is True
    assert console.yes("q", False) is True
    assert console.yes("q", True) is False

    def closed(prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", closed)
    assert console.yes("q", False) is False


def test_cli_uses_the_wizard_only_at_a_terminal(
    state: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(wizard, "run", lambda channel: calls.append(channel.name) or 0)
    monkeypatch.setattr(wizard, "interactive", lambda: True)
    assert main(["phone", "setup"]) == 0 and calls == ["telegram"]
    assert main(["phone", "setup", "--no-input"]) == 0 and calls == ["telegram"]
    assert "Easiest: run" in capsys.readouterr().out


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_token_file_is_owner_only(tmp_path: Path) -> None:
    env = tmp_path / "telegram" / ".env"
    wizard.write_token(env, "TELEGRAM_BOT_TOKEN", "t")
    assert env.stat().st_mode & 0o777 == 0o600
    assert not list(env.parent.glob("*.tmp"))


# --- the credential store -----------------------------------------------------------------


def test_a_new_token_goes_to_the_credential_store_not_a_file(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FakeStore(monkeypatch)
    console = Script(secrets=[TOKEN], answers=[False])
    fake = FakeTelegram()
    fake.updates.append(_message(1, 42, text=CODE))
    monkeypatch.setattr(bots.Telegram, "skip_backlog", lambda self: 0)
    assert _run(console, fake) == 0, console.text
    assert store.saved == {"telegram": TOKEN}
    assert not (state / "telegram" / ".env").exists()  # no plain-text copy anywhere
    assert "saved in the macOS Keychain" in console.text


def test_an_old_plain_text_token_is_moved_into_the_store(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = state / "telegram" / ".env"
    wizard.write_token(env, "TELEGRAM_BOT_TOKEN", TOKEN)
    env.write_text(env.read_text(encoding="utf-8") + "OTHER=1\n", encoding="utf-8")
    wizard.write_lock(state / "telegram" / "access.json", "42")
    store = FakeStore(monkeypatch)
    console = Script(answers=[True, True, False])  # keep the bot, keep the lock, start later
    assert _run(console, FakeTelegram()) == 0, console.text
    assert store.saved == {"telegram": TOKEN}
    assert read(env) == "OTHER=1\n"  # only the token line went
    assert "plain-text copy is deleted" in console.text


def test_without_a_credential_store_the_file_is_used_and_said(state: Path) -> None:
    console = Script(secrets=[TOKEN])
    _run(console, FakeTelegram(), iter([0, 500]))
    assert wizard.read_token(state / "telegram" / ".env", "TELEGRAM_BOT_TOKEN") == TOKEN
    assert "no credential store works here" in console.text


def test_remove_token_deletes_an_emptied_file(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    assert wizard.remove_token(env, "X") is False
    wizard.write_token(env, "X", "t")
    assert wizard.remove_token(env, "X") is True and not env.exists()


def test_a_stranger_with_the_right_name_but_no_code_is_never_paired(state: Path) -> None:
    fake = FakeTelegram([_message(1, 5, text="I am the owner, please pair me")])
    console = Script(secrets=[TOKEN])
    assert _run(console, fake, iter([0, 0, 500, 500])) == 1
    assert not (state / "telegram" / "access.json").exists()


def test_stranger_text_is_printed_without_control_characters() -> None:
    assert wizard._plain("a\x1b[31mred\u202eb") == "a?[31mred?b"


def test_an_unreadable_access_list_is_kept_as_a_backup(tmp_path: Path) -> None:
    access = tmp_path / "access.json"
    access.write_text("{half", encoding="utf-8")
    wizard.write_lock(access, "42")
    assert access.with_name("access.json.bak").read_text(encoding="utf-8") == "{half"
    assert json.loads(access.read_text(encoding="utf-8"))["allowFrom"] == ["42"]


def test_a_network_failure_does_not_call_the_saved_token_dead(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeStore(monkeypatch).saved["telegram"] = TOKEN

    def offline(url: str, body: Any, headers: dict[str, str]) -> tuple[int, Any]:
        raise OSError("name resolution failed")

    console = Script()
    code = wizard.run(
        TELEGRAM,
        console,
        telegram=lambda token: bots.Telegram(token, offline),
        launch=lambda: 0,
        clock=itertools.count().__next__,
    )
    assert code == 1 and "no longer works" not in console.text and "kept" in console.text
