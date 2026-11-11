"""What the plugin directory checks: literal hook paths and no bin/. (`claude plugin
validate` checks the icon; naming the image here would hold the plugin for review.)"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK_COMMAND = re.compile(r'^cd "\$\{CLAUDE_PLUGIN_ROOT\}" ; \./scripts/hooks/run\.cmd ([a-z-]+)$')
WRAPPER = re.compile(
    r'^exec "\$BASH" "\$\{CLAUDE_PLUGIN_ROOT\}/scripts/sdd\.sh" (hook-[a-z-]+)$', re.MULTILINE
)


def _hooks() -> list[dict[str, object]]:
    data = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    return [hook for groups in data["hooks"].values() for g in groups for hook in g["hooks"]]


def test_every_hook_runs_a_literal_shell_script_through_claude_codes_shell() -> None:
    """The directory follows a hook's shell script one level and refuses computed paths in
    it. Each hook therefore runs a one-line wrapper with only literal paths; the launcher it
    starts (which has to find Python) is held for a reviewer instead of refused."""
    hooks = _hooks()
    assert hooks
    for hook in hooks:
        # String form, so Claude Code's own shell resolves `bash`. In exec form Windows looks
        # in System32 first and finds the WSL launcher (bash.exe), which fails without a distro.
        assert "args" not in hook, hook
        found = HOOK_COMMAND.match(str(hook["command"]))
        assert found, f"hooks go through scripts/hooks/run.cmd: {hook['command']}"
        script = f"scripts/hooks/{found.group(1)}.sh"
        wrapper = (ROOT / script).read_text(encoding="utf-8")
        code = [line for line in wrapper.splitlines() if line and not line.startswith("#")]
        assert len(code) == 1 and WRAPPER.match(code[0]), wrapper
        name = Path(script).stem
        assert WRAPPER.match(code[0]).group(1) == f"hook-{name}"  # type: ignore[union-attr]


def test_no_bin_folder() -> None:
    # Claude Code puts bin/ on PATH, and claude.ai and Cowork refuse plugins that have it.
    assert not (ROOT / "bin").exists()


def test_the_runner_never_trusts_a_bare_bash_on_windows() -> None:
    """A bare `bash` on Windows can be System32\bash.exe, the WSL launcher, which fails
    with "execvpe(/bin/bash) failed" when no distribution is installed."""
    runner = (ROOT / "scripts" / "hooks" / "run.cmd").read_text(encoding="utf-8")
    block = runner.split("CMDBLOCK")[1]  # the part cmd.exe runs
    assert "System32" in block and "WindowsApps" in block  # both stubs are skipped by name
    assert '"%GITBASH%" "%HOOK_SH%"' in block
    assert b"\r" not in (ROOT / "scripts" / "hooks" / "run.cmd").read_bytes()  # bare LF only


def test_the_runner_runs_under_cmd_exe() -> None:
    """cmd.exe has to parse the batch half of the file as it is stored, with LF endings."""
    import subprocess
    import sys

    import pytest

    if sys.platform != "win32":
        pytest.skip("cmd.exe only")
    runner = ROOT / "scripts" / "hooks" / "run.cmd"
    proc = subprocess.run(["cmd.exe", "/c", str(runner)], capture_output=True, text=True)
    assert proc.returncode == 0
    assert "needs a hook name" in proc.stderr
    assert "is not recognized" not in proc.stderr + proc.stdout


def test_the_runner_and_wrappers_are_executable_in_git() -> None:
    import subprocess

    listing = subprocess.run(
        ["git", "ls-files", "-s", "scripts/hooks"], cwd=ROOT, capture_output=True, text=True
    ).stdout
    assert listing and all(line.startswith("100755") for line in listing.splitlines())
