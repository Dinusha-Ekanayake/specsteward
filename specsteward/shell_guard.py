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
from fnmatch import fnmatchcase
from typing import Any

from specsteward.remote import CHANNELS, state_dir

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
        # PowerShell cmdlets and the aliases people actually type
        "set-content",
        "add-content",
        "clear-content",
        "out-file",
        "remove-item",
        "move-item",
        "copy-item",
        "rename-item",
        "new-item",
        "set-itemproperty",
        "sc",
        "ac",
        "del",
        "erase",
        "ri",
        "mi",
        "ni",
        "cpi",
        "rni",
        "move",
        "copy",
        "ren",
        "rename",
        "xcopy",
        "robocopy",
    ]
)
ENV_DUMPS = frozenset(("env", "printenv", "set", "declare", "compgen"))
SEGMENT = re.compile(r"&&|\|\||[;|&\n]|\$\(|`")
PROTECTED = re.compile(
    r"(?:^|[\s'\"=/\\])\.sdd(?:[/\\](?!bin[/\\]sdd\b)|['\"\s]|$)"  # run state, not the shim
    r"|\.claude[/\\]specsteward"  # trust store, approvals, encrypted token
    r"|\.claude[/\\]settings(?:\.local)?\.json"  # the permission rules that make the gates
    r"|\.claude[/\\]channels[/\\][^/\\\s]+[/\\]access\.json"  # who may answer from the phone
    r"|/proc/[^\s]*/environ",  # the process environment, token included
    re.IGNORECASE,
)
SECRET = re.compile(
    r"\.claude[/\\]channels[/\\][^/\\\s]+[/\\]\.env|\.claude[/\\]specsteward[/\\]secrets"
    r"|\b(?:TELEGRAM|DISCORD)_BOT_TOKEN\b|/proc/[^\s]*/environ",
    re.IGNORECASE,
)
REDIRECT_INTO = re.compile(r">>?\s*(\S+)")
REDIRECT_TAIL = re.compile(r">>?.*", re.DOTALL)


PREFIXES = frozenset(
    ("env", "command", "nohup", "time", "xargs", "sudo", "doas", "exec", "builtin", "stdbuf")
    + ("nice", "setsid", "timeout")
)
# Options of a prefix command that take a separate argument (`sudo -u root rm ...`).
PREFIX_ARGS = {
    "sudo": frozenset(("-u", "-g", "-C", "-h", "-p", "-r", "-t", "-U", "--user", "--group")),
    "doas": frozenset(("-u", "-C")),
    "env": frozenset(("-u", "-C", "-S", "--unset", "--chdir")),
    "timeout": frozenset(("-s", "-k", "--signal", "--kill-after")),
    "nice": frozenset(("-n", "--adjustment")),
    "stdbuf": frozenset(("-i", "-o", "-e")),
    "xargs": frozenset(("-I", "-n", "-P", "-L", "-s", "-d", "-E")),
}
LOOKUPS = frozenset(("-v", "-V"))  # `command -v bash` only looks a name up
CD = frozenset(("cd", "pushd", "chdir", "set-location", "sl"))
INTERPRETERS = frozenset(("python", "python3", "py", "node", "perl", "ruby", "php", "deno"))
CODE_FLAG = re.compile(r"-[a-zA-Z]*[cepr]|--eval|--print")
RISKY_CODE = re.compile(
    r"specsteward|\.sdd|environ|getenv|settings|trust|subprocess|os\.system|__import__"
    r"|b64decode|base64|\bexec\b|\beval\b|chr\(|['\"]\s*\+\s*['\"]",
    re.IGNORECASE,
)
STDIN_SHELL = "a shell is reading commands from a pipe or heredoc, which cannot be inspected"


def _squash(text: str) -> str:
    """The text as the shell will see the path: quotes, escapes and backticks removed, so
    `.s''dd` and `.s\\dd` read as `.sdd`."""
    return re.sub(r"[\'\"`\\]", "", text)


def _glob_hits(text: str) -> bool:
    """A word with wildcards that could expand to a protected name (`.sd?`, `~/.claude/specs*`)."""
    names = (".sdd", ".claude", "specsteward", "settings.json", "state.json", "trust.json")
    for part in re.split(r"[\s/=<>]+", _squash(text)):
        literals = re.sub(r"[*?\[\]]", "", part)
        wildcard = len(literals) >= 2 and re.search(r"[*?\[]", part)
        if wildcard and any(fnmatchcase(name, part) for name in names):
            return True
    return False


def _custom_state(text: str) -> bool:
    flat = _squash(text).replace("\\", "/").lower()
    return any(state_dir(channel).as_posix().lower() in flat for channel in CHANNELS)


def _protected(text: str) -> bool:
    return bool(
        PROTECTED.search(text)
        or PROTECTED.search(_squash(text))
        or _glob_hits(text)
        or _custom_state(text)
    )


def _has_script(words: list[str]) -> bool:
    """False for a shell that will read its commands from stdin (`bash`, `bash -s`)."""
    rest = [word for word in words[1:] if not word.startswith("<")]
    if "-s" in rest:
        return False  # `bash -s -- arg` still reads its commands from stdin
    return any(not word.startswith(("-", "/")) for word in rest) or any(
        _is_inline(word) for word in rest
    )


def _is_inline(word: str) -> bool:
    lowered = word.lower()
    return lowered in INLINE or re.fullmatch(r"-[a-z]*c", lowered) is not None


def _words(segment: str) -> list[str]:
    try:
        words = shlex.split(segment, posix=True)
    except ValueError:  # unbalanced quotes: fall back to a plain split
        words = segment.split()
    while words:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
            words = words[1:]  # VAR=value prefixes
        elif _name(words[0]) in PREFIXES and len(words) > 1:
            prefix = _name(words[0])
            if prefix in ("command", "builtin") and words[1] in LOOKUPS:
                break
            takes_arg = PREFIX_ARGS.get(prefix, frozenset())
            words = words[1:]
            while words and (words[0].startswith("-") or re.fullmatch(r"\d+[smhd]?", words[0])):
                skip = 2 if words[0] in takes_arg else 1
                words = words[skip:]  # the prefix's own options (`env -i`, `sudo -u root`)
        else:
            break
    return words


_EXTENSIONS = (".exe", ".cmd", ".bat", ".ps1", ".com")


def _name(word: str) -> str:
    name = re.split(r"[/\\]", word)[-1].lower()
    for extension in _EXTENSIONS:
        if name.endswith(extension):
            return name[: -len(extension)]
    return name


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


SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "pwsh", "powershell", "cmd"})
EVALS = frozenset({"eval", "iex", "invoke-expression"})
ENCODED = ("-e", "-ec", "-enc", "-encodedcommand")
INLINE = ("-c", "-command", "/c", "/k", "-commandwithargs")
OPAQUE = "an encoded command cannot be inspected"


def _nested(words: list[str]) -> str:
    """The command a nested shell (`bash -c "..."`, `powershell -Command ...`, `cmd /c ...`)
    or `eval` / `Invoke-Expression` runs, else "". An encoded command comes back as OPAQUE."""
    name = _name(words[0])
    if name in EVALS:
        return " ".join(words[1:])
    if name not in SHELLS:
        return ""
    lowered = [word.lower() for word in words]
    if name in ("pwsh", "powershell") and any(flag in lowered[1:] for flag in ENCODED):
        return OPAQUE
    for index, word in enumerate(lowered[1:], start=1):
        if _is_inline(word) and index + 1 < len(words):
            # bash takes one string; PowerShell and cmd take everything that follows
            rest = words[index + 1 :]
            return rest[0] if name in ("bash", "sh", "zsh", "dash", "ksh") else " ".join(rest)
    return ""


GATE_TEXT = re.compile(r"sdd\W.*\b(?:approve|trust|start|resume|signoff)\b", re.IGNORECASE)
CHANGES_STATE = "this command may change run state, the trust store or the permission rules"


def _reason(command: str) -> str:
    whole = _words(command.strip())
    inner = _nested(whole) if whole else ""
    if inner == OPAQUE:
        return OPAQUE
    if inner and inner != command and (found := _reason(inner)):
        return found  # `bash -c '...; sdd approve'`: look inside before splitting on `;`
    if (
        whole
        and _name(whole[0]) in SHELLS
        and "<<" in command
        and GATE_TEXT.search(_squash(command))
    ):
        return "`sdd` is a human gate; only the user may approve it"
    in_protected = False  # after `cd .sdd`, a bare `state.json` is a protected file
    for segment in SEGMENT.split(command):
        words = _words(segment.strip())
        if not words:
            continue
        name = _name(words[0])
        if name in SHELLS and not _has_script(words):
            return STDIN_SHELL
        inner = _nested(words)
        if inner == OPAQUE:
            return OPAQUE
        if inner and inner != command and (found := _reason(inner)):
            return found
        gated = _gated_sdd(words)
        if gated:
            return f"`sdd {gated}` is a human gate; only the user may approve it"
        if SECRET.search(segment) or SECRET.search(_squash(segment)):
            return "this command touches the phone bot token"
        tokens = {channel.token_var for channel in CHANNELS}
        dumps = name in ENV_DUMPS or words[:2] == ["export", "-p"]
        if dumps and len(words) <= 2 and tokens & os.environ.keys():
            return "this session holds the phone bot token; listing the environment shows it"
        if name in CD and _protected(" ".join(words[1:])):
            in_protected = True
        redirects = REDIRECT_INTO.findall(segment)
        tail = REDIRECT_TAIL.search(segment)
        writes = name in WRITERS or bool(tail and _protected(tail.group()))
        if writes and (_protected(segment) or (in_protected and name not in CD)):
            return CHANGES_STATE
        if in_protected and redirects:
            return CHANGES_STATE
        if name in INTERPRETERS:
            args = [w for w in words[1:] if not w.startswith("<")]
            no_script = not any(not w.startswith("-") for w in args)
            reads_stdin = not args or "-" in args or "<<" in segment
            if no_script and reads_stdin and not any(CODE_FLAG.fullmatch(w) for w in args):
                return STDIN_SHELL  # `python - <<EOF`, `cat x | python`
            if any(CODE_FLAG.fullmatch(w) for w in args) and RISKY_CODE.search(command):
                return "inline code that touches run state, the environment or its own name"
    return ""


def shell_guard_decision(payload: dict[str, Any]) -> dict[str, Any] | None:
    if payload.get("tool_name") not in (None, "Bash", "PowerShell"):
        return None
    command = str((payload.get("tool_input") or {}).get("command") or "")
    if payload.get("tool_name") == "PowerShell":
        # A backslash is a path separator there, not an escape, and a backtick is the escape.
        command = command.replace(chr(92), "/").replace(chr(96), "")
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
