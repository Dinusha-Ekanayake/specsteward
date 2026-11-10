"""Phone control through Anthropic's official channel plugins: Telegram or Discord.

specsteward does not run its own bot. It reads a channel plugin's allowlist to know which
chat to notify, and checks that the setup is safe (allowlist policy, no strangers). In
both plugins a DM's chat id equals the sender's user id, so ``allowFrom`` is the chat list.
"""

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from specsteward import secret_store


@dataclass(frozen=True)
class Channel:
    name: str  # also the plugin name and its /<name>:access and /<name>:configure skills
    token_var: str
    dir_var: str
    group_word: str  # what the plugin calls the places beyond DMs


CHANNELS = (
    Channel("telegram", "TELEGRAM_BOT_TOKEN", "TELEGRAM_STATE_DIR", "groups"),
    Channel("discord", "DISCORD_BOT_TOKEN", "DISCORD_STATE_DIR", "server channels"),
)
TELEGRAM = CHANNELS[0]


def env_without_tokens() -> dict[str, str]:
    """This environment minus the phone bot tokens, for any child process that has no use
    for them (checks, formatters, installers, the graph builder, the claude CLI)."""
    secret = {channel.token_var for channel in CHANNELS}
    return {key: value for key, value in os.environ.items() if key not in secret}


def state_dir(channel: Channel = TELEGRAM) -> Path:
    default = Path.home() / ".claude" / "channels" / channel.name
    return Path(os.environ.get(channel.dir_var) or default)


def _token_in(env_file: Path, var: str) -> bool:
    """A non-empty ``<VAR>=`` line, not just an .env file that exists."""
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    return any(
        line.strip().startswith(f"{var}=") and line.split("=", 1)[1].strip() for line in lines
    )


def configured(channel: Channel) -> bool:
    """The user has started this channel's setup: a token or a pairing exists."""
    directory = state_dir(channel)
    return (
        bool(os.environ.get(channel.token_var))
        or bool(secret_store.stored_in(channel.name))
        or any((directory / name).exists() for name in (".env", "access.json"))
    )


def channel_status(channel: Channel, directory: Path | None = None) -> dict[str, Any]:
    directory = directory or state_dir(channel)
    access_file = directory / "access.json"
    token_set = (
        bool(os.environ.get(channel.token_var))
        or bool(secret_store.stored_in(channel.name))
        or _token_in(directory / ".env", channel.token_var)
    )
    name = channel.name
    if not access_file.exists():
        return {
            "channel": name,
            "ready": False,
            "chat_ids": [],
            "problems": [f"not paired yet: DM the bot, then /{name}:access pair <code>"],
            "token_set": token_set,
        }
    access = json.loads(access_file.read_text(encoding="utf-8"))
    chat_ids = [str(i) for i in access.get("allowFrom", [])]
    problems = []
    if not token_set:
        problems.append(
            f"no bot token: run `bash .sdd/bin/sdd phone setup --channel {name}` in a terminal"
        )
    if not chat_ids:
        problems.append("allowlist is empty: pair your account first")
    if len(chat_ids) > 1:
        problems.append("more than one user can approve; keep only yours for gates")
    if access.get("dmPolicy", "pairing") != "allowlist":
        problems.append(f"dmPolicy is not 'allowlist': run /{name}:access policy allowlist")
    if access.get("groups"):
        problems.append(f"bot is in {channel.group_word}; gate messages go to DMs only")
    return {
        "channel": name,
        "ready": not problems,
        "chat_ids": chat_ids,
        "problems": problems,
        "token_set": token_set,
    }


def telegram_status(directory: Path | None = None) -> dict[str, Any]:
    return channel_status(TELEGRAM, directory)


def _command_lines() -> list[str]:
    """Command lines of the running processes; [] when they cannot be listed."""
    if sys.platform == "win32":
        query = "Get-CimInstance Win32_Process | ForEach-Object { $_.CommandLine }"
        cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", query]
    else:
        cmd = ["ps", "-A", "-o", "args="]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return proc.stdout.splitlines() if proc.returncode == 0 else []


def running_servers(lines: list[str] | None = None) -> dict[str, int]:
    """How many channel servers run, per channel. Each Claude Code session with the plugin
    enabled starts one, and a bot delivers each message to only one of them."""
    lines = _command_lines() if lines is None else lines
    counts: dict[str, int] = {}
    for channel in CHANNELS:
        pattern = re.compile(rf"bun(\.exe)?\s+run\b.*[\\/]{channel.name}[\\/][^\s\"]*")
        found = sum(1 for line in lines if pattern.search(line))
        if found:
            counts[channel.name] = found
    return counts


def remote_status() -> dict[str, Any]:
    """The channel to use (the first ready one, else the first one set up, else Telegram),
    with the others listed under ``others``."""
    statuses = [channel_status(channel) for channel in CHANNELS]
    set_up = [s for s, c in zip(statuses, CHANNELS, strict=True) if configured(c)]
    ready = [s for s in statuses if s["ready"]]
    chosen = (ready or set_up or statuses)[0]
    others = [s["channel"] for s in statuses if s is not chosen and (s["ready"] or s in set_up)]
    return {**chosen, "others": others}
