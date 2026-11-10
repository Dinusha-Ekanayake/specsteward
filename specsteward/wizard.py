"""`sdd phone setup` in a terminal: a guided setup that leaves only the steps a person must do.

Telegram: paste the token, send the bot one message, confirm it was you. The wizard saves
the token where the plugin reads it, writes your account to the allowlist and locks the
bot, sends a test message and can start `claude-phone`. Pairing is confirmed on your own
keyboard, never from a chat message. Discord: the token is checked and saved; pairing
still happens through the plugin, because reading Discord DMs needs a live gateway.
"""

import getpass
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from specsteward import bots, phone, secret_store
from specsteward.remote import Channel, running_servers, state_dir

PAIR_SECONDS = 180
TOKEN_ATTEMPTS = 3


class Console:
    """Terminal input and output; tests pass a scripted stand-in."""

    def say(self, text: str = "") -> None:
        print(text, flush=True)

    def secret(self, prompt: str) -> str:
        return getpass.getpass(prompt)

    def yes(self, question: str, default: bool) -> bool:
        hint = "[Y/n]" if default else "[y/N]"
        try:
            answer = input(f"{question} {hint} ").strip().lower()
        except EOFError:
            return default
        return default if not answer else answer in ("y", "yes")


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


# --- files the plugin reads ---------------------------------------------------------------


def read_token(env_file: Path, var: str) -> str:
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and key.strip() == var:
            return value.strip()
    return ""


def _other_lines(env_file: Path, var: str) -> list[str]:
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [line for line in lines if line.partition("=")[0].strip() != var]


def write_token(env_file: Path, var: str, token: str) -> None:
    """The fallback when there is no credential store: ``VAR=token`` in the plugin's own
    file, owner-only from creation, keeping any other lines."""
    lines = [*_other_lines(env_file, var), f"{var}={token}"]
    secret_store.write_private(env_file, "\n".join(lines) + "\n")


def remove_token(env_file: Path, var: str) -> bool:
    """Drop the plain-text token once the credential store holds it. True if one was there."""
    if not read_token(env_file, var):
        return False
    kept = _other_lines(env_file, var)
    if any(line.strip() for line in kept):
        secret_store.write_private(env_file, "\n".join(kept) + "\n")
    else:
        env_file.unlink()
    return True


def _access(access_file: Path) -> dict[str, object]:
    try:
        data = json.loads(access_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def locked_account(access_file: Path) -> str:
    """The one account the bot is locked to, or ""."""
    access = _access(access_file)
    allowed = access.get("allowFrom")
    if access.get("dmPolicy") == "allowlist" and isinstance(allowed, list) and len(allowed) == 1:
        return str(allowed[0])
    return ""


def write_lock(access_file: Path, user_id: str) -> int:
    """Only ``user_id`` may use the bot. Keeps group settings; returns accounts removed."""
    if access_file.exists() and not _access(access_file) and access_file.stat().st_size:
        access_file.replace(access_file.with_name(access_file.name + ".bak"))  # keep what was there
    access = _access(access_file)
    before = access.get("allowFrom")
    removed = len([i for i in before if str(i) != user_id]) if isinstance(before, list) else 0
    access.update(dmPolicy="allowlist", allowFrom=[user_id], pending={})
    access.setdefault("groups", {})
    secret_store.write_private(access_file, json.dumps(access, indent=2) + "\n")
    return removed


# --- steps --------------------------------------------------------------------------------


def _bot_help(channel: Channel) -> list[str]:
    if channel.name == "telegram":
        return [
            "Create a bot: open https://t.me/BotFather in Telegram, send /newbot,",
            "pick a name and a username ending in 'bot', and copy the token it sends.",
        ]
    return [
        "Create a bot: https://discord.com/developers/applications -> New Application ->",
        "Bot -> Reset Token (copy it), and turn on Message Content Intent. Then invite it:",
        "OAuth2 -> URL Generator -> scope 'bot' -> open the URL.",
    ]


def _check(channel: Channel, token: str, telegram: Callable[[str], bots.Telegram]) -> str:
    if channel.name == "telegram":
        return telegram(token).username()
    return bots.discord_username(token)


def _save(channel: Channel, token: str, console: Console) -> None:
    """Into the credential store; the owner-only file only where there is none."""
    env_file = state_dir(channel) / ".env"
    try:
        kind = secret_store.store(channel.name, token)
    except ValueError:
        kind = None
    if kind:
        moved = remove_token(env_file, channel.token_var)
        extra = "; the plain-text copy is deleted" if moved else ""
        console.say(f"ok   token saved in {secret_store.LABELS[kind]}{extra}")
        return
    write_token(env_file, channel.token_var, token)
    where = (
        "plain text, protected only by your user profile folder"
        if sys.platform == "win32"
        else "owner-only"
    )
    console.say(f"warn no credential store works here; token saved ({where}) to {env_file}")
    if sys.platform.startswith("linux"):
        console.say("     to keep it encrypted: install secret-tool (libsecret) and a keyring")


def _token(
    channel: Channel, console: Console, telegram: Callable[[str], bots.Telegram]
) -> tuple[str, str] | None:
    """(token, bot username), saved in the credential store, or None."""
    env_file = state_dir(channel) / ".env"
    in_file = read_token(env_file, channel.token_var)
    existing = secret_store.load(channel.name) or in_file
    if existing:
        try:
            name = _check(channel, existing, telegram)
        except bots.BotError as error:
            if error.unreachable:
                console.say(f"FAIL {error}; the saved token was kept, try again when online")
                return None
            console.say(f"warn the saved token no longer works: {error}")
        else:
            if console.yes(f"Use the bot you already set up, @{name}?", True):
                if in_file:  # move it out of the plain-text file
                    _save(channel, existing, console)
                return existing, name
    console.say()
    for line in _bot_help(channel):
        console.say(line)
    for _ in range(TOKEN_ATTEMPTS):
        token = console.secret("Paste the bot token (it stays hidden): ").strip()
        if not bots.valid_token(channel.name, token):
            console.say(f"That does not look like a {channel.name} bot token; try again.")
            continue
        try:
            name = _check(channel, token, telegram)
        except bots.BotError as error:
            console.say(f"FAIL {error}")
            continue
        console.say(f"ok   connected to @{name}")
        _save(channel, token, console)
        return token, name
    console.say("FAIL no working token; run `sdd phone setup` again when you have one")
    return None


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")


def _pairing_code() -> str:
    return secrets.token_hex(3)


def _plain(text: str, limit: int = 40) -> str:
    """Text from a stranger, safe to print: no escape or bidi characters, cut short."""
    return _CONTROL.sub("?", text)[:limit]


def _wait_for_you(
    api: bots.Telegram, bot: str, console: Console, clock: Callable[[], float]
) -> str | None:
    offset = api.skip_backlog()
    code = _pairing_code()
    console.say()
    console.say(f"Now send this code to @{bot} in Telegram: {code}")
    console.say("(only the message with exactly this code pairs; waiting up to 3 minutes)")
    deadline = clock() + PAIR_SECONDS
    while clock() < deadline:
        offset, sender = api.next_private_message(offset)
        if sender is None:
            continue
        if sender.text.strip().lower() == code:
            console.say(f"ok   code received from account {sender.user_id}")
            api.acknowledge(offset)
            return sender.user_id
        console.say(
            f"Ignored a message from account {_plain(sender.user_id)} "
            "(wrong code). Waiting for yours..."
        )
    api.acknowledge(offset)
    console.say("FAIL no message arrived; run `sdd phone setup` again")
    return None


def _pair_telegram(
    token: str,
    bot: str,
    console: Console,
    telegram: Callable[[str], bots.Telegram],
    clock: Callable[[], float],
) -> bool:
    access_file = state_dir(phone.channel_named("telegram")) / "access.json"
    locked = locked_account(access_file)
    if locked and console.yes(f"The bot is locked to account {locked}. Keep that?", True):
        return True
    api = telegram(token)
    try:
        user_id = _wait_for_you(api, bot, console, clock)
        if user_id is None:
            return False
        removed = write_lock(access_file, user_id)
        extra = f" ({removed} other account(s) removed)" if removed else ""
        console.say(f"ok   locked: only account {user_id} can use @{bot}{extra}")
        api.send(
            user_id,
            "specsteward connected. Start Claude Code with claude-phone, then send: "
            "status, approve <NNN>, answer: ..., resume, ship <NNN>, stop.",
        )
        console.say("ok   sent a test message to your phone")
    except bots.BotError as error:
        console.say(f"FAIL {error}")
        return False
    return True


def _old_copies(channel: Channel, console: Console) -> None:
    count = running_servers().get(channel.name, 0)
    if count:
        console.say(
            f"warn {count} copy(ies) of the {channel.name} bot still run in Claude Code "
            "windows opened before setup; close or restart those windows"
        )


def _bash() -> str:
    """bash for the launcher; on Windows not the WSL stub in System32, which cannot run it."""
    found = shutil.which("bash")
    if found and sys.platform == "win32" and "system32" in found.lower().replace("\\", "/"):
        for root in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)")):
            candidate = Path(root or "") / "Git" / "bin" / "bash.exe"
            if root and candidate.exists():
                return str(candidate)
        return "bash"
    return found or "bash"


def launch_phone() -> int:
    script = phone.launcher_dir() / phone.LAUNCHER
    return subprocess.call([_bash(), str(script)])  # noqa: S603 - our own launcher


def run(
    channel: Channel,
    console: Console | None = None,
    *,
    telegram: Callable[[str], bots.Telegram] = bots.Telegram,
    launch: Callable[[], int] = launch_phone,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    console = console or Console()
    ok, lines = phone.setup(channel)
    for line in lines:
        console.say(line)
    if not ok:
        return 1
    _old_copies(channel, console)
    found = _token(channel, console, telegram)
    if found is None:
        return 1
    token, bot = found
    if channel.name == "telegram":
        if not _pair_telegram(token, bot, console, telegram, clock):
            return 1
    else:
        console.say()
        console.say("Last steps, in a `claude-phone` session: DM the bot, run")
        console.say(
            "/discord:access pair <code>, then `bash .sdd/bin/sdd phone lock --channel discord`."
        )
    console.say()
    if console.yes("Start phone control now (claude-phone)?", True):
        return launch()
    console.say(f"Start it any time with `{phone.LAUNCHER}` instead of `claude`.")
    return 0
