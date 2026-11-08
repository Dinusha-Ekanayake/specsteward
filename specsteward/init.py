"""``sdd init``: set up a project with a config preset, templates and permission rules."""

import json
import shutil
from pathlib import Path
from typing import Any

from specsteward import trust
from specsteward.config import CONFIG_FILE
from specsteward.presets import detect_stack, preset_for, render_checks, render_format
from specsteward.safefs import ensure_safe

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
SDD = "bash .sdd/bin/sdd"

ALLOW = [
    f"Bash({SDD}:*)",
    "Bash(git status:*)",
    "Bash(git diff:*)",
    "Bash(git log:*)",
    "Bash(git rev-parse:*)",
]
# Human gates. "ask" beats "allow", so these always prompt, and a channel plugin
# with permission relay (for example Telegram) can show the prompt on a phone.
# Everything that approves, trusts, or resets a cap is here; halting and stopping are not.
ASK = [
    f"Bash({SDD} approve:*)",
    f"Bash({SDD} trust:*)",
    f"Bash({SDD} start:*)",  # a restart would reset the caps
    f"Bash({SDD} resume:*)",  # clears a halt and the per-task counters
    f"Bash({SDD} signoff:*)",  # the user's judgement on a human task
    "Bash(git commit:*)",
    "Bash(git push:*)",
    "Bash(gh pr create:*)",
    "Bash(gh pr merge:*)",
]

# Defence in depth next to the PreToolUse guard hook: file tools never edit run state
# or the trust store. "Edit" rules cover every built-in file-editing tool.
DENY = [
    "Edit(.sdd/**)",
    "Edit(~/.claude/specsteward/**)",
    "Edit(.claude/settings.json)",
    "Edit(.claude/settings.local.json)",
]


def render_config(root: Path, preset: str = "auto", specs_dir: str = "specs") -> str:
    """``sdd.toml`` for this project, with the gate built from what it already uses."""
    chosen = preset_for(root, preset)
    text = (TEMPLATES / CONFIG_FILE).read_text(encoding="utf-8")
    scan = "[" + ", ".join(f'"{name}"' for name in chosen.scan) + "]"
    for marker, value in (
        ("@@STACK@@", chosen.stack),
        ("@@SPECS_DIR@@", specs_dir),
        ("@@CHECKS@@", render_checks(chosen)),
        ("@@FORMAT@@", render_format(chosen)),
        ("@@SCAN@@", scan),
    ):
        text = text.replace(marker, value)
    return text


def _merge_settings(path: Path) -> list[str]:
    settings: dict[str, Any] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    permissions = settings.setdefault("permissions", {})
    # On a re-run (upgrades), add only the sdd gates and deny rules: the git rules are the
    # user's to relax, and removing one must not be undone by the next `init`.
    rerun = ALLOW[0] in permissions.get("allow", [])
    added = []
    for key, rules in (("allow", ALLOW), ("ask", ASK), ("deny", DENY)):
        existing = permissions.setdefault(key, [])
        for rule in rules:
            if rerun and key != "deny" and ".sdd/bin/sdd" not in rule:
                continue
            if rule not in existing:
                existing.append(rule)
                added.append(f"{key}: {rule}")
    ensure_safe(path.parent.parent, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return added


# The plugin declares no hooks (the plugin directory refuses command hooks), so `init`
# registers them in the project's own settings, where the user owns and can review them.
HOOK_RUNNER = 'bash "$CLAUDE_PROJECT_DIR/.sdd/bin/sdd"'
PROJECT_HOOKS: tuple[tuple[str, str, str, int], ...] = (
    ("SessionStart", "", "hook-session-start", 30),
    ("PreToolUse", "Edit|Write|MultiEdit|NotebookEdit", "hook-guard", 30),
    ("PreToolUse", "Bash", "hook-shell-guard", 30),
    ("PostToolUse", "Edit|Write|MultiEdit", "hook-format", 60),
    ("PostToolUse", "Bash", "hook-bind", 30),
    ("Stop", "", "hook-stop", 30),
    ("SubagentStop", "", "hook-subagent-stop", 30),
)


def _merge_hooks(path: Path) -> list[str]:
    """Add the specsteward hooks to a settings file; hooks already there are kept."""
    settings: dict[str, Any] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    hooks = settings.setdefault("hooks", {})
    added = []
    for event, matcher, name, timeout in PROJECT_HOOKS:
        command = f"{HOOK_RUNNER} {name}"
        groups = hooks.setdefault(event, [])
        if any(h.get("command") == command for g in groups for h in g.get("hooks", [])):
            continue
        entry = {"type": "command", "command": command, "timeout": timeout}
        group: dict[str, Any] = {"hooks": [entry]}
        if matcher:
            group["matcher"] = matcher
        groups.append(group)
        added.append(name)
    if added:
        ensure_safe(path.parent.parent, path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return added


STATUSLINE = {"type": "command", "command": f"{SDD} statusline", "padding": 0}


def enable_statusline(root: Path) -> str:
    """Opt-in: show the run in this user's status line (``.claude/settings.local.json``)."""
    path = root / ".claude" / "settings.local.json"
    settings: dict[str, Any] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if "statusLine" in settings:
        return f"kept the existing status line; add `{SDD} statusline` to it to show the run"
    settings["statusLine"] = STATUSLINE
    ensure_safe(root, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return "status line enabled in .claude/settings.local.json"


def _ensure_gitignore(root: Path) -> bool:
    path = root / ".gitignore"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if any(line.strip() in (".sdd", ".sdd/") for line in text.splitlines()):
        return False
    prefix = "" if not text or text.endswith("\n") else "\n"
    ensure_safe(root, path)
    path.write_text(f"{text}{prefix}\n# specsteward run state\n.sdd/\n", encoding="utf-8")
    return True


def init_project(
    root: Path, preset: str = "auto", specs_dir: str = "specs", force: bool = False
) -> list[str]:
    """Create what is missing and report each change. Never overwrites without ``force``."""
    done = []
    config = root / CONFIG_FILE
    if config.exists() and not force:
        done.append(f"kept existing {CONFIG_FILE}")
        if not trust.config_trusted(root):
            done.append(f"review {CONFIG_FILE}, then run `{SDD} trust` before the first check")
    else:
        replacing = config.exists()
        stack = detect_stack(root) if preset == "auto" else preset
        ensure_safe(root, config)
        config.write_text(render_config(root, stack, specs_dir), encoding="utf-8")
        steps = ", ".join(step.name for step in preset_for(root, stack).checks) or "none found"
        if replacing:
            # `init --force` is allow-listed, so replacing a tuned config must not also
            # trust the (weaker) default: the user reviews it and runs `sdd trust`.
            done.append(f"replaced {CONFIG_FILE} ({stack} project; checks: {steps}; NOT trusted)")
            done.append(f"review {CONFIG_FILE}, then run `{SDD} trust` before the first check")
        else:
            trust.trust_config(root)  # a new file the user asked for starts trusted
            done.append(f"wrote {CONFIG_FILE} ({stack} project; checks: {steps}; trusted)")
            if steps == "none found":
                done.append(f"add your test command under [[check]] in {CONFIG_FILE} before a run")
    constitution = root / specs_dir / "constitution.md"
    if not constitution.exists():
        ensure_safe(root, constitution)
        constitution.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(TEMPLATES / "constitution.md", constitution)
        done.append(f"wrote {constitution.relative_to(root).as_posix()} (edit the examples)")
    for folder, source in (("_template", TEMPLATES), ("_template_fix", TEMPLATES / "fix")):
        template_dir = root / specs_dir / folder
        template_dir.mkdir(parents=True, exist_ok=True)
        for name in ("spec.md", "plan.md", "tasks.md"):
            target = template_dir / name
            if not target.exists() or force:
                ensure_safe(root, target)
                shutil.copyfile(source / name, target)
                done.append(f"wrote {target.relative_to(root).as_posix()}")
    if _ensure_gitignore(root):
        done.append("added .sdd/ to .gitignore")
    rules = _merge_settings(root / ".claude" / "settings.json")
    labels = {"allow": "runs without asking", "ask": "asks you first", "deny": "blocked"}
    for key in ("allow", "ask", "deny"):
        added = [rule.split(": ", 1)[1] for rule in rules if rule.startswith(f"{key}: ")]
        if added:
            done.append(f"permission rules ({labels[key]}): {', '.join(added)}")
    hooks = _merge_hooks(root / ".claude" / "settings.json")
    if hooks:
        done.append(f"hooks (in .claude/settings.json): {', '.join(hooks)}")
    return done
