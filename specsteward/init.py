"""``sdd init``: set up a project with a config preset, templates and permission rules."""

import json
import re
import shutil
import tomllib
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

# Commands that install software, rewrite launchers or create worktrees: not gates, but
# nothing an agent should start without the user seeing it. (`ask` beats the broad allow.)
EXTRA_ASK = [f"Bash({SDD} {name}:*)" for name in ("phone", "install", "worktree", "init")]

# Claude Code on Windows runs shell commands with the PowerShell tool, which the Bash rules
# above do not cover, so the same gates are repeated for it.
POWERSHELL_ASK = [rule.replace("Bash(", "PowerShell(", 1) for rule in ASK]
POWERSHELL_EXTRA_ASK = [rule.replace("Bash(", "PowerShell(", 1) for rule in EXTRA_ASK]
POWERSHELL_ALLOW = [rule.replace("Bash(", "PowerShell(", 1) for rule in ALLOW[:1]]

# Defence in depth next to the PreToolUse guard hook: file tools never edit run state
# or the trust store. "Edit" rules cover every built-in file-editing tool.
DENY = [
    "Edit(.sdd/**)",
    "Edit(~/.claude/specsteward/**)",
    "Edit(.claude/settings.json)",
    "Edit(.claude/settings.local.json)",
]


_SPECS_DIR = re.compile(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*")


SCRIPT_RUNNERS = frozenset(
    ("npm", "pnpm", "yarn", "bun", "npx", "make", "just", "task", "rake", "tox", "nox", "hatch")
    + ("gradle", "gradlew", "mvn", "mvnw", "poetry", "pdm", "uv", "sh", "bash", "cargo-make")
)


def runs_repo_scripts(root: Path) -> bool:
    """True when a check or format command starts the project's own scripts (`npm run lint`,
    `make test`), whose bodies the repository controls, rather than a fixed tool."""
    try:
        with (root / CONFIG_FILE).open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, ValueError):
        return True
    commands = [step.get("cmd", []) for step in raw.get("check", [])]
    for formats in raw.get("format", {}).values():
        commands.extend(formats)
    for cmd in commands:
        first = str(cmd[0]).replace("\\", "/").rsplit("/", 1)[-1].lower() if cmd else ""
        if first in SCRIPT_RUNNERS or first.startswith(("./", "../")):
            return True
    return False


def check_specs_dir(specs_dir: str) -> str:
    """A relative folder of plain names: it ends up inside sdd.toml and on disk."""
    parts = specs_dir.split("/")
    if not _SPECS_DIR.fullmatch(specs_dir) or ".." in parts or "." in parts:
        raise ValueError(
            f"--specs-dir must be a plain relative folder such as specs, got {specs_dir!r}"
        )
    return specs_dir


def render_config(root: Path, preset: str = "auto", specs_dir: str = "specs") -> str:
    """``sdd.toml`` for this project, with the gate built from what it already uses."""
    check_specs_dir(specs_dir)
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
    for key, rules in (
        ("allow", [*ALLOW, *POWERSHELL_ALLOW]),
        ("ask", [*ASK, *EXTRA_ASK, *POWERSHELL_ASK, *POWERSHELL_EXTRA_ASK]),
        ("deny", DENY),
    ):
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


# The plugin declares its own hooks (hooks/hooks.json). Releases 0.3.2 wrote the same hooks
# into the project's settings; a project still carrying them would run every hook twice
# (and count each agent twice), so `init` removes those older entries.
LEGACY_HOOK_PREFIX = 'bash "$CLAUDE_PROJECT_DIR/.sdd/bin/sdd" hook-'
LEGACY_HOOKS = frozenset(
    f"{LEGACY_HOOK_PREFIX}{name}"
    for name in (
        "session-start",
        "guard",
        "shell-guard",
        "format",
        "bind",
        "stop",
        "subagent-stop",
    )
)


def _is_legacy(hook: Any) -> bool:
    return isinstance(hook, dict) and hook.get("command") in LEGACY_HOOKS


def legacy_hook_commands(settings: Any) -> list[str]:
    """The 0.3.2 project-level specsteward hook commands found in a parsed settings file."""
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    if not isinstance(hooks, dict):
        return []
    return [
        str(hook["command"])
        for groups in hooks.values()
        if isinstance(groups, list)
        for group in groups
        if isinstance(group, dict) and isinstance(group.get("hooks"), list)
        for hook in group["hooks"]
        if _is_legacy(hook)
    ]


def _without_legacy(groups: list[Any]) -> list[Any]:
    kept_groups = []
    for group in groups:
        entries = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(entries, list) or not any(_is_legacy(h) for h in entries):
            kept_groups.append(group)  # not ours: leave exactly as the user wrote it
            continue
        kept = [h for h in entries if not _is_legacy(h)]
        if kept:
            kept_groups.append({**group, "hooks": kept})
    return kept_groups


def _drop_legacy_project_hooks(path: Path) -> list[str]:
    """Remove the 0.3.2 project-level specsteward hooks; anything else is left untouched."""
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []  # missing or unreadable: nothing of ours to remove
    removed = legacy_hook_commands(settings)
    if not removed:
        return []
    hooks = settings["hooks"]
    for event, groups in list(hooks.items()):
        if isinstance(groups, list):
            kept_groups = _without_legacy(groups)
            if kept_groups:
                hooks[event] = kept_groups
            elif len(kept_groups) != len(groups):
                del hooks[event]
    if not hooks:
        del settings["hooks"]
    ensure_safe(path.parent.parent, path)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return [command.removeprefix(LEGACY_HOOK_PREFIX) for command in removed]


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
    check_specs_dir(specs_dir)
    done = []
    config = root / CONFIG_FILE
    if config.exists() and not force:
        done.append(f"kept existing {CONFIG_FILE}")
        if not trust.config_trusted(root):
            done.append(f"review {CONFIG_FILE}, then run `{SDD} trust` before the first check")
    else:
        # A project the trust store already knows is never auto-trusted: otherwise deleting
        # a tuned sdd.toml and running the allow-listed `init` would re-trust the weaker default.
        replacing = config.exists() or trust.is_known(root)
        stack = detect_stack(root) if preset == "auto" else preset
        ensure_safe(root, config)
        config.write_text(render_config(root, stack, specs_dir), encoding="utf-8")
        steps = ", ".join(step.name for step in preset_for(root, stack).checks) or "none found"
        if replacing:
            # `init --force` is allow-listed, so replacing a tuned config must not also
            # trust the (weaker) default: the user reviews it and runs `sdd trust`.
            done.append(f"replaced {CONFIG_FILE} ({stack} project; checks: {steps}; NOT trusted)")
            done.append(f"review {CONFIG_FILE}, then run `{SDD} trust` before the first check")
        elif runs_repo_scripts(root):
            done.append(f"wrote {CONFIG_FILE} ({stack} project; checks: {steps}; NOT trusted)")
            done.append(
                f"its commands run scripts from this repository; review {CONFIG_FILE}, "
                f"then run `{SDD} trust`"
            )
        else:
            trust.trust_config(root)  # fixed tool commands the user asked for start trusted
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
    legacy = _drop_legacy_project_hooks(root / ".claude" / "settings.json")
    if legacy:
        done.append(f"removed project hooks the plugin now provides: {', '.join(legacy)}")
    return done
