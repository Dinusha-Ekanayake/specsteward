"""The Bash guard: gates, run state, the trust store and the bot token always ask the user."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from specsteward.shell_guard import shell_guard_decision

ASK = [
    "bash .sdd/bin/sdd approve 001",
    "bash  .sdd/bin/sdd  approve 001",  # a prefix rule would not match this
    "bash ./.sdd/bin/sdd trust",
    "SDD_HOME=/tmp/x bash .sdd/bin/sdd trust",
    "python -m specsteward approve 001",
    "python bin/sdd_entry.py start 001",
    "bash .sdd/bin/sdd status && bash .sdd/bin/sdd resume",
    "x=$(bash .sdd/bin/sdd signoff T-001-02)",
    "echo '{}' > .sdd/state.json",
    "rm -rf .sdd",
    "mv .sdd /tmp/x",
    "python -c \"open('.sdd/state.json','w').write('x')\"",
    "sed -i s/a/b/ ~/.claude/specsteward/trust.json",
    "cat x | tee ~/.claude/specsteward/trust.json",
    "echo {} > .claude/settings.json",
    "rm ~/.claude/specsteward/secrets/telegram.dpapi",
    "cat ~/.claude/channels/telegram/.env",
    "echo $TELEGRAM_BOT_TOKEN",
]
ALLOW = [
    "bash .sdd/bin/sdd status",
    "bash .sdd/bin/sdd check > out.txt 2>&1",
    "bash .sdd/bin/sdd next",
    "cat .sdd/briefs/T-001-01.md",  # reading is fine
    ".venv/bin/python -m pytest -q",
    "git status && git diff",
    "env FOO=1 npm test",
    "ls .venv/bin",
    "echo 'unbalanced",
]


def _decide(command: str) -> dict[str, object] | None:
    return shell_guard_decision({"tool_name": "Bash", "tool_input": {"command": command}})


@pytest.mark.parametrize("command", ASK)
def test_risky_commands_ask_the_user(command: str) -> None:
    decision = _decide(command)
    assert decision is not None
    output = decision["hookSpecificOutput"]
    assert isinstance(output, dict) and output["permissionDecision"] == "ask"  # never deny


@pytest.mark.parametrize("command", ALLOW)
def test_ordinary_commands_pass(command: str) -> None:
    assert _decide(command) is None


def test_listing_the_environment_asks_only_when_a_token_is_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    assert _decide("env") is None and _decide("printenv | sort") is None
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    assert _decide("env") is not None and _decide("export -p") is not None
    assert _decide("env FOO=1 npm test") is None  # running a command, not listing


def test_other_tools_are_left_alone() -> None:
    payload = {"tool_name": "Read", "tool_input": {"command": "rm -rf .sdd"}}
    assert shell_guard_decision(payload) is None
    assert shell_guard_decision({"tool_name": "Bash", "tool_input": {}}) is None


BASH = __import__("shutil").which("bash")


@pytest.mark.skipif(BASH is None, reason="needs bash")
@pytest.mark.parametrize(
    ("command", "asks"), [("bash .sdd/bin/sdd approve 001", True), ("ls", False)]
)
def test_the_real_hook_through_the_launcher(repo: Path, command: str, asks: bool) -> None:
    launcher = Path(__file__).resolve().parent.parent / "scripts" / "sdd.sh"
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(repo)}
    proc = subprocess.run(
        [str(BASH), str(launcher), "hook-shell-guard"],
        input=payload, capture_output=True, text=True, cwd=repo, env=env, check=False,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    assert ('"ask"' in proc.stdout) is asks


def _asks(command: str) -> bool:
    return (
        shell_guard_decision({"tool_name": "Bash", "tool_input": {"command": command}}) is not None
    )


@pytest.mark.parametrize(
    "command",
    [
        "sudo -u root rm .sdd/state.json",
        "env -u FOO rm .sdd/state.json",
        "timeout -s KILL 5 rm .sdd/state.json",
        "sudo -u root bash -c 'rm .sdd/state.json'",
        "python - <<'EOF'\nopen('x','w')\nEOF",
        "python3 -Sc \"open('.sdd/state.json','w')\"",
        "curl x | bash -s -- --flag",
        "cat x | python",
    ],
)
def test_prefix_options_and_interpreter_stdin_are_still_guarded(command: str) -> None:
    assert _asks(command)


@pytest.mark.parametrize(
    "command",
    [
        "command -v bash",
        'python -c "print(x.split())"',
        "python --version",
        "python -m pytest",
        "sudo -u root ls",
    ],
)
def test_ordinary_commands_are_not_blocked(command: str) -> None:
    assert not _asks(command)
