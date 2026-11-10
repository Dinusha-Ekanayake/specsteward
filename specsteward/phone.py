"""`sdd phone`: one-command setup of phone control, and one bot per machine.

Every Claude Code session with a channel plugin enabled (other terminals, VS Code panels)
starts its own copy of the bot, and Telegram or Discord hands each message to only one of
them, so replies get lost. The cure is to keep the plugin off by default and load it
only in the session meant for the phone: `claude --plugin-dir <its folder>` loads a
plugin that is off elsewhere for that one session (`--settings` cannot switch a disabled
plugin back on; both verified live), and `--channels` passes its messages into the
session. `sdd phone setup` does everything that needs no human, and installs a
`claude-phone` command that starts such a session.
"""

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from specsteward import prereqs, secret_store
from specsteward.remote import CHANNELS, Channel, env_without_tokens, state_dir

MARKETPLACE = "claude-plugins-official"
LAUNCHER = "claude-phone"
CLAUDE_TIMEOUT = 300

Claude = Callable[[list[str]], tuple[int, str]]


def plugin_id(channel: Channel) -> str:
    return f"{channel.name}@{MARKETPLACE}"


def channel_named(name: str) -> Channel:
    for channel in CHANNELS:
        if channel.name == name:
            return channel
    raise ValueError(f"channel must be one of {[c.name for c in CHANNELS]}, got {name!r}")


def _claude(args: list[str]) -> tuple[int, str]:
    exe = shutil.which("claude")
    if exe is None:
        return 127, "the `claude` command was not found on PATH"
    try:
        proc = subprocess.run(
            [exe, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CLAUDE_TIMEOUT,
            check=False,
            stdin=subprocess.DEVNULL,
            env=env_without_tokens(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return 1, str(error)
    return proc.returncode, proc.stdout + proc.stderr


def plugin_state(channel: Channel, claude: Claude = _claude) -> dict[str, object] | None:
    """The plugin's entry from `claude plugin list --json`, or None if not installed."""
    code, out = claude(["plugin", "list", "--json"])
    if code != 0:
        return None
    try:
        entries = json.loads(out[out.index("[") :])
    except ValueError:
        return None
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and entry.get("id") == plugin_id(channel):
            return entry
    return None


def launcher_dir() -> Path:
    """Where the `claude` command itself usually lives, so it is already on PATH."""
    claude = shutil.which("claude")
    return Path(claude).parent if claude else Path.home() / ".local" / "bin"


LAUNCHER_SCRIPT = r"""#!/usr/bin/env bash
# Written by specsteward (`sdd phone setup`): Claude Code with {name} phone control.
# The {name} plugin is off in ordinary sessions, so only sessions started here run the bot.
# It is loaded for this session with --plugin-dir (newest installed version), and
# --channels passes your messages into the session.
set -euo pipefail
# From cmd or PowerShell, Git Bash starts without its own tools on PATH (Windows' sort.exe
# would answer instead), so the standard Unix tool folders go first.
export PATH="/usr/bin:/bin:$PATH"
base="${{CLAUDE_CONFIG_DIR:-$HOME/.claude}}/plugins/cache/{marketplace}/{name}"
dir="$(ls -d "$base"/*/ 2>/dev/null | sort -V | tail -n 1 || true)"
if [ -z "$dir" ]; then
  echo "{launcher}: the {name} plugin is not installed; run \`sdd phone setup\`." >&2
  exit 1
fi
# The bot token comes from the operating system's credential store (`sdd phone setup` put
# it there) and reaches the plugin as an environment variable, never a plain-text file.
secrets="{secrets}"
if [ -z "${{{var}:-}}" ] && [ -f "$secrets/{name}.store" ]; then
  token=""
  case "$(cat "$secrets/{name}.store")" in
    dpapi)
      token="$(SPECSTEWARD_BLOB="$secrets/{name}.dpapi" powershell.exe -NoProfile \
        -NonInteractive -Command '{powershell}' 2>/dev/null | tr -d '\r' || true)" ;;
    keychain)
      token="$(security find-generic-password -s {service} -a {account} -w \
        2>/dev/null || true)" ;;
    secret-service)
      token="$(secret-tool lookup service {service} account {account} 2>/dev/null || true)" ;;
  esac
  if [ -z "$token" ]; then
    echo "{launcher}: could not read the {name} bot token from the credential store;" \
      "run \`sdd phone setup\` again." >&2
    exit 1
  fi
  export {var}="$token"
fi
# Tells specsteward's shell guard that this session holds a bot token (it never reads it).
export SDD_PHONE_SESSION=1
exec claude --plugin-dir "$dir" --channels plugin:{plugin} "$@"
"""


def write_launcher(channel: Channel, directory: Path | None = None) -> list[Path]:
    """`claude-phone` (bash) and, on Windows, `claude-phone.cmd` that runs it from cmd or
    PowerShell. The plugin folder is looked up each time, so plugin updates do not break it.
    """
    directory = directory or launcher_dir()
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / LAUNCHER
    text = LAUNCHER_SCRIPT.format(
        name=channel.name,
        marketplace=MARKETPLACE,
        plugin=plugin_id(channel),
        launcher=LAUNCHER,
        var=channel.token_var,
        secrets=secret_store.secrets_dir().as_posix(),
        service=secret_store.service(channel.name),
        account=secret_store.ACCOUNT,
        powershell=secret_store.POWERSHELL_READ,
    )
    script.write_text(text, encoding="utf-8", newline="\n")
    script.chmod(0o755)
    written = [script]
    if sys.platform == "win32":
        cmd = directory / f"{LAUNCHER}.cmd"
        # The full path of the bash running setup (Git Bash): from cmd or PowerShell a bare
        # `bash` can resolve to WSL's bash.exe in System32 instead.
        bash = shutil.which("bash") or "bash"
        cmd.write_text(f'@echo off\r\n"{bash}" "%~dp0{LAUNCHER}" %*\r\n', encoding="utf-8")
        written.append(cmd)
    return written


def setup(channel: Channel, claude: Claude = _claude) -> tuple[bool, list[str]]:
    """Everything that needs no human. Returns (ok, lines to show)."""
    lines: list[str] = []
    ok, message = prereqs.install_bun()
    lines.append(f"{'ok  ' if ok else 'FAIL'} Bun: {message}")
    if not ok:
        return False, lines
    state = plugin_state(channel, claude)
    if state is None:
        code, out = claude(["plugin", "install", plugin_id(channel), "--scope", "user"])
        if code != 0:
            lines.append(f"FAIL installing {plugin_id(channel)}: {out.strip()[-300:]}")
            return False, lines
        lines.append(f"ok   installed {plugin_id(channel)}")
        state = plugin_state(channel, claude) or {}
    else:
        lines.append(f"ok   {plugin_id(channel)} is installed")
    if state.get("enabled"):
        code, out = claude(["plugin", "disable", plugin_id(channel), "--scope", "user"])
        if code != 0:
            lines.append(
                f"FAIL switching {plugin_id(channel)} off by default: {out.strip()[-300:]}"
            )
            return False, lines
        lines.append(
            f"ok   {channel.name} is now off in ordinary sessions (no more competing bots)"
        )
    else:
        lines.append(f"ok   {channel.name} is off in ordinary sessions")
    for path in write_launcher(channel):
        lines.append(f"ok   wrote {path}")
    return True, lines


def next_steps(channel: Channel) -> list[str]:
    configure = f"/{channel.name}:configure <token>"
    if channel.name == "telegram":
        bot = "In Telegram, message @BotFather, send /newbot and copy the token."
    else:
        bot = (
            "In the Discord Developer Portal create an application and a bot, turn on "
            "Message Content Intent, copy the token and invite the bot."
        )
    return [
        "Easiest: run `bash .sdd/bin/sdd phone setup` in a terminal of your own; it asks for",
        "the token, pairs and locks the bot, and starts the phone session. Or by hand, once:",
        f"  1. {bot}",
        f"  2. Exit Claude Code and start it with `{LAUNCHER}` (instead of `claude`).",
        f"  3. In that session: {configure}",
        f"  4. DM the bot; it answers with a code. Run /{channel.name}:access pair <code>.",
        "  5. Run `bash .sdd/bin/sdd phone lock` so only your account can use the bot.",
        f"From then on, start Claude Code with `{LAUNCHER}` whenever you want phone control.",
    ]


def lock(channel: Channel, directory: Path | None = None) -> str:
    """Set dmPolicy to allowlist once exactly one account is paired."""
    access_file = (directory or state_dir(channel)) / "access.json"
    if not access_file.is_file():
        raise ValueError(f"not paired yet: DM the bot, then /{channel.name}:access pair <code>")
    access = json.loads(access_file.read_text(encoding="utf-8"))
    paired = [str(i) for i in access.get("allowFrom", [])]
    if len(paired) != 1:
        raise ValueError(
            f"expected exactly one paired account, found {len(paired)}; "
            f"fix it with /{channel.name}:access, then lock again"
        )
    if access.get("dmPolicy") == "allowlist":
        return f"{channel.name} is already locked to account {paired[0]}"
    access["dmPolicy"] = "allowlist"
    tmp = access_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(access, indent=2) + "\n", encoding="utf-8")
    tmp.chmod(0o600)  # the allowlist decides who can answer prompts: owner only
    os.replace(tmp, access_file)
    return f"{channel.name} locked: only account {paired[0]} can use the bot"


def enabled_everywhere(channel: Channel, claude: Claude = _claude) -> bool:
    """The plugin is on in every session, so each one starts its own copy of the bot."""
    state = plugin_state(channel, claude)
    return bool(state and state.get("enabled"))
