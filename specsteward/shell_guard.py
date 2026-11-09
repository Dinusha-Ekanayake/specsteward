"""PreToolUse guard for Bash: make Claude Code ask the user before shell commands that could
move a gate, change run state or the trust store, or read the phone bot token.

The permission rules already ask for `bash .sdd/bin/sdd approve` and friends, but a rule is
a prefix match: `bash  .sdd/bin/sdd approve` (two spaces), `bash ./.sdd/bin/sdd approve`
or `python -m specsteward approve` would fall under the broad allow rule instead. This hook
recognises those however they are spelled. It never denies: it returns "ask", so the user
always decides, and ordinary commands are not touched.
"""

import os
import re
import shlex
from typing import Any

from specsteward.remote import CHANNELS

GATED = ("approve", "trust", "start", "resume", "signoff")
WRITERS = frozenset(
    [
        "rm",
        "rmdir",
        "mv",
        "cp",
        "tee",
        "touch",
        "truncate",
        "chmod",
        "chown",
        "ln",
        "dd",
        "install",
        "rsync",
        "sed",
        "perl",
        "python",
        "python3",
        "py",
        "node",
        "ruby",
        "bash",
        "sh",
        "zsh",
        "pwsh",
        "powershell",
        "powershell.exe",
        "cmd",
        "cmd.exe",
    ]
)
ENV_DUMPS = frozenset(("env", "printenv", "set", "declare", "compgen"))
SEGMENT = re.compile(r"&&|\|\||[;|&\n]|\$\(|`")
PROTECTED = re.compile(
    r"(?:^|[\s'\"=/\\])\.sdd(?:[/\\](?!bin[/\\]sdd\b)|['\"\s]|$)"  # run state, not the shim
    r"|\.claude[/\\]specsteward"  # trust store, approvals, encrypted token
    r"|\.claude[/\\]settings(?:\.local)?\.json"  # the permission rules that make the gates
    r"|\.claude[/\\]channels[/\\][^/\\\s]+[/\\]access\.json"  # who may answer from the phone
)
SECRET = re.compile(
    r"\.claude[/\\]channels[/\\][^/\\\s]+[/\\]\.env|\.claude[/\\]specsteward[/\\]secrets"
    r"|\b(?:TELEGRAM|DISCORD)_BOT_TOKEN\b"
)
REDIRECT_INTO = re.compile(r">>?\s*['\"]?([^\s'\";&|]+)")


def _words(segment: str) -> list[str]:
    try:
        words = shlex.split(segment, posix=True)
    except ValueError:  # unbalanced quotes: fall back to a plain split
        words = segment.split()
    while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
        words = words[1:]  # VAR=value prefixes
    return words


def _name(word: str) -> str:
    return re.split(r"[/\\]", word)[-1].lower()


def _gated_sdd(words: list[str]) -> str:
    """The gated subcommand when these words run sdd in any form, else ""."""
    for i, word in enumerate(words):
        runs_sdd = _name(word) in ("sdd", "sdd_entry.py") or (
            word == "-m" and i + 1 < len(words) and words[i + 1].startswith("specsteward")
        )
        if not runs_sdd:
            continue
        rest = [w for w in words[i + 1 :] if not w.startswith("-")]
        if rest and rest[0].startswith("specsteward"):
            rest = rest[1:]
        if rest and rest[0] in GATED:
            return rest[0]
    return ""


SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "pwsh", "powershell"})


def _nested(words: list[str]) -> str:
    """The command string a nested shell (`bash -c "..."`) or `eval` runs, else ""."""
    name = _name(words[0])
    if name == "eval":
        return " ".join(words[1:])
    if name in SHELLS and "-c" in words[:-1]:
        return words[words.index("-c") + 1]
    return ""


def _reason(command: str) -> str:
    whole = _words(command.strip())
    inner = _nested(whole) if whole else ""
    if inner and inner != command and (found := _reason(inner)):
        return found  # `bash -c '...; sdd approve'`: look inside before splitting on `;`
    for segment in SEGMENT.split(command):
        words = _words(segment.strip())
        if not words:
            continue
        inner = _nested(words)
        if inner and inner != command and (found := _reason(inner)):
            return found
        gated = _gated_sdd(words)
        if gated:
            return f"`sdd {gated}` is a human gate; only the user may approve it"
        if SECRET.search(segment):
            return "this command touches the phone bot token"
        tokens = {channel.token_var for channel in CHANNELS}
        dumps = _name(words[0]) in ENV_DUMPS or words[:2] == ["export", "-p"]
        if dumps and len(words) <= 2 and tokens & os.environ.keys():
            return "this session holds the phone bot token; listing the environment shows it"
        writes = _name(words[0]) in WRITERS or any(
            PROTECTED.search(target) for target in REDIRECT_INTO.findall(segment)
        )
        if writes and PROTECTED.search(segment):
            return "this command may change run state, the trust store or the permission rules"
    return ""


def shell_guard_decision(payload: dict[str, Any]) -> dict[str, Any] | None:
    if payload.get("tool_name") not in (None, "Bash"):
        return None
    command = str((payload.get("tool_input") or {}).get("command") or "")
    reason = _reason(command) if command else ""
    if not reason:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": f"specsteward: {reason}.",
        }
    }
