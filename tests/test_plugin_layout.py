"""What the plugin directory checks: literal hook paths and no bin/. (`claude plugin
validate` checks the icon; naming the image here would hold the plugin for review.)"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = re.compile(r"^\$\{CLAUDE_PLUGIN_ROOT\}/(scripts/hooks/[\w-]+\.sh)$")
WRAPPER = re.compile(
    r'^exec bash "\$\{CLAUDE_PLUGIN_ROOT\}/scripts/sdd\.sh" (hook-[a-z-]+)$', re.MULTILINE
)


def _hooks() -> list[dict[str, object]]:
    data = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    return [hook for groups in data["hooks"].values() for g in groups for hook in g["hooks"]]


def test_every_hook_runs_a_literal_shell_script_in_exec_form() -> None:
    """The directory follows a hook's shell script one level and refuses computed paths in
    it. Each hook therefore runs a one-line wrapper with only literal paths; the launcher it
    starts (which has to find Python) is held for a reviewer instead of refused."""
    hooks = _hooks()
    assert hooks
    for hook in hooks:
        assert hook["command"] == "bash", hook  # exec form: no shell parses the line
        args = hook["args"]
        assert isinstance(args, list) and len(args) == 1, hook
        script = HOOK_SCRIPT.match(str(args[0]))
        assert script, f"path must be spelled literally from the plugin root: {args[0]}"
        wrapper = (ROOT / script.group(1)).read_text(encoding="utf-8")
        code = [line for line in wrapper.splitlines() if line and not line.startswith("#")]
        assert len(code) == 1 and WRAPPER.match(code[0]), wrapper
        name = Path(script.group(1)).stem
        assert WRAPPER.match(code[0]).group(1) == f"hook-{name}"  # type: ignore[union-attr]


def test_no_bin_folder() -> None:
    # Claude Code puts bin/ on PATH, and claude.ai and Cowork refuse plugins that have it.
    assert not (ROOT / "bin").exists()
