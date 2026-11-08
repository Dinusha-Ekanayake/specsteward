"""What the plugin directory checks: no hooks and no bin/. (`claude plugin
validate` checks the icon; naming the image here would hold the plugin for review.)"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_plugin_declares_no_hooks() -> None:
    """The directory refuses command hooks; `sdd init` writes them to project settings."""
    assert not (ROOT / "hooks").exists()
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert "hooks" not in manifest


def test_no_bin_folder() -> None:
    # Claude Code puts bin/ on PATH, and claude.ai and Cowork refuse plugins that have it.
    assert not (ROOT / "bin").exists()
