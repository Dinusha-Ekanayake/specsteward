"""``sdd doctor``: check everything a run depends on, and say how to fix what is missing."""

import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from specsteward import config as cfg_mod
from specsteward import graph, phone, prereqs, secret_store, trust, wizard
from specsteward.check import resolve_cmd
from specsteward.hooks import SHIM
from specsteward.init import ASK, DENY, EXTRA_ASK, POWERSHELL_ASK, legacy_hook_commands
from specsteward.remote import CHANNELS, channel_status, configured, running_servers, state_dir
from specsteward.state import current_branch

OK, WARN, FAIL = "ok", "warn", "FAIL"
MIN_PYTHON = (3, 11)


@dataclass(frozen=True)
class Finding:
    level: str
    what: str
    fix: str = ""


def _python() -> Finding:
    version = ".".join(map(str, sys.version_info[:3]))
    if sys.version_info[:2] >= MIN_PYTHON:
        return Finding(OK, f"Python {version}")
    return Finding(FAIL, f"Python {version} is too old", "install Python 3.11+ or set SDD_PYTHON")


def _git(root: Path) -> Finding:
    if shutil.which("git") is None:
        return Finding(WARN, "git not found", "install git; branch checks and diffs need it")
    try:
        branch = current_branch(root)  # also works before the first commit
    except ValueError:
        return Finding(
            WARN,
            "not a git repository",
            "run `git init`; reviews read `git diff` and the scope check compares against git",
        )
    return Finding(OK, f"git branch {branch}")


def _checks(cfg: cfg_mod.Config) -> list[Finding]:
    if not trust.config_trusted(cfg.root):
        return [Finding(FAIL, "sdd.toml is not trusted", "review sdd.toml, then `sdd trust`")]
    found = [Finding(OK, "sdd.toml is trusted")]
    if not cfg.check:
        fix = 'add your test command to sdd.toml: [[check]] name = "tests" cmd = [...]'
        found.append(Finding(FAIL, "the gate has no [[check]] steps, so it proves nothing", fix))
    missing: dict[str, list[str]] = {}
    for step in cfg.check:
        try:
            exe = Path(resolve_cmd(step.cmd, cfg.root)[0]).name
        except FileNotFoundError as error:
            missing.setdefault(str(error), []).append(step.name)
        else:
            found.append(Finding(OK, f"check {step.name!r} runs {exe}"))
    for tool, steps in missing.items():
        needed = ", ".join(repr(s) for s in steps)
        found.append(Finding(FAIL, f"{tool} not found (needed by {needed})", f"install {tool}"))
    return found


def _settings(root: Path) -> list[dict[str, Any]]:
    files = (
        root / ".claude" / "settings.json",
        root / ".claude" / "settings.local.json",
        Path.home() / ".claude" / "settings.json",
    )
    out = []
    for path in files:
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def _duplicate_hooks(root: Path) -> list[Finding]:
    """Hooks that releases 0.3.2 wrote into the project now come from the plugin."""
    names = sorted({c for s in _settings(root) for c in legacy_hook_commands(s)})
    if not names:
        return []
    return [
        Finding(
            WARN,
            f"{len(names)} specsteward hooks are in .claude/settings.json and in the plugin, "
            "so each runs twice",
            "run `/specsteward:init` once to remove the project copies",
        )
    ]


def _permissions(root: Path) -> list[Finding]:
    settings = _settings(root)
    rules = {
        key: {rule for s in settings for rule in s.get("permissions", {}).get(key, [])}
        for key in ("ask", "deny")
    }
    found = []
    missing = [rule for rule in ASK if rule not in rules["ask"]]
    gates = [rule for rule in missing if ".sdd/bin/sdd" in rule]
    git = [rule for rule in missing if rule not in gates]
    extra = [rule for rule in EXTRA_ASK if rule not in rules["ask"]]
    if extra:
        found.append(
            Finding(
                WARN,
                f"these install or create things without asking: {extra}",
                "run `sdd init` again to add the rules",
            )
        )
    ps_missing = [rule for rule in POWERSHELL_ASK[:5] if rule not in rules["ask"]]
    if ps_missing:
        level = FAIL if sys.platform == "win32" else WARN
        found.append(
            Finding(
                level,
                f"the PowerShell tool is not covered by the sdd gates: {ps_missing}",
                "run `sdd init` again",
            )
        )
    if gates:
        found.append(Finding(FAIL, f"sdd gates do not prompt: {gates}", "run `sdd init` again"))
    else:
        found.append(Finding(OK, "approve, trust, start and resume prompt you"))
    if git:
        found.append(Finding(WARN, f"these run without asking: {git}", "fine if you chose it"))
    missing_deny = [rule for rule in DENY if rule not in rules["deny"]]
    if missing_deny:
        found.append(Finding(WARN, f"deny rules missing: {missing_deny}", "run `sdd init` again"))
    if not any((s.get("sandbox") or {}).get("enabled") for s in settings):
        found.append(
            Finding(
                WARN,
                "sandbox is off: `sdd check` runs agent-written tests as you",
                'enable Claude Code sandboxing ("sandbox": {"enabled": true})',
            )
        )
    for var in ("SDD_HOME", "SDD_PYTHON"):
        if any(var in (s.get("env") or {}) for s in settings):
            found.append(
                Finding(WARN, f"{var} is set in Claude settings", "make sure you set it yourself")
            )
    modes = {s.get("permissions", {}).get("defaultMode") for s in settings}
    if "bypassPermissions" in modes:
        found.append(
            Finding(FAIL, "defaultMode is bypassPermissions", "gates do not prompt; change it")
        )
    return found


def _install(root: Path) -> list[Finding]:
    found = []
    shim = root / SHIM
    if shim.exists():
        found.append(Finding(OK, f"{SHIM.as_posix()} exists"))
    else:
        found.append(Finding(WARN, f"{SHIM.as_posix()} missing", "run `sdd init` or restart"))
    gitignore = root / ".gitignore"
    text = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    if ".sdd" not in text:
        found.append(Finding(WARN, ".sdd/ is not git-ignored", "run `sdd init` again"))
    return found


def _graph(cfg: cfg_mod.Config) -> Finding:
    if not cfg.graphify_auto:
        return Finding(
            WARN, "code graph is off ([graphify] auto = false)", "agents read more files"
        )
    if graph.find_graphify() is None:
        return Finding(WARN, "graphify not installed yet", "`sdd graph sync` installs it")
    status = graph.graph_status(cfg.root)
    if status == "fresh":
        return Finding(OK, "code graph is up to date")
    return Finding(WARN, f"code graph is {status}", "`sdd graph sync` (also runs at session start)")


def _tools() -> list[Finding]:
    found = []
    if shutil.which("gh"):
        found.append(Finding(OK, "GitHub CLI (gh) found"))
    else:
        found.append(Finding(WARN, "GitHub CLI (gh) not found", "install it to open PRs on ship"))
    if graph.find_graphify() is None and not (shutil.which("uv") or shutil.which("pipx")):
        found.append(
            Finding(WARN, "neither uv nor pipx found", "graphify falls back to pip --user")
        )
    return found


def _remote() -> list[Finding]:
    if not prereqs.phone_control_configured():
        return [Finding(OK, "phone control not set up (optional: `/specsteward:remote`)")]
    found = []
    if prereqs.find_bun() is None:
        found.append(Finding(FAIL, "Bun is missing; phone control needs it", "`sdd install bun`"))
    elif not prereqs.bun_on_path():
        fix = "restart the terminal and Claude Code"
        found.append(Finding(WARN, "Bun is installed but not on PATH", fix))
    else:
        found.append(Finding(OK, "Bun found"))
    servers = running_servers()
    for channel in CHANNELS:
        if not configured(channel):
            continue
        count = servers.get(channel.name, 0)
        if count > 1:
            found.append(
                Finding(
                    WARN,
                    f"{count} {channel.name} servers are running (one per Claude Code session); "
                    "each reply reaches only one of them",
                    f"`sdd phone setup --channel {channel.name}`, restart the other sessions, "
                    "and use `claude-phone` for phone control",
                )
            )
        store = secret_store.backend()
        if store != "file" and wizard.read_token(state_dir(channel) / ".env", channel.token_var):
            found.append(
                Finding(
                    WARN,
                    f"the {channel.name} bot token is in a plain-text file",
                    f"`sdd phone setup --channel {channel.name}` in a terminal moves it into "
                    f"{secret_store.LABELS[store]}",
                )
            )
        if phone.enabled_everywhere(channel):
            found.append(
                Finding(
                    WARN,
                    f"the {channel.name} plugin is on in every Claude Code session, "
                    "so each one runs its own copy of the bot",
                    f"`sdd phone setup --channel {channel.name}`, then use `claude-phone`",
                )
            )
        status = channel_status(channel)
        title = channel.name.capitalize()
        if status["ready"]:
            found.append(Finding(OK, f"{title} phone control ready"))
        else:
            problems = "; ".join(status["problems"])
            found.append(Finding(WARN, f"{title}: {problems}", "`/specsteward:remote`"))
    return found


def run_doctor(start: Path, extra: list[Finding] | None = None) -> list[Finding]:
    found = [_python()]
    try:
        cfg = cfg_mod.load(start)
    except ValueError as error:
        return [*found, Finding(FAIL, str(error), "run `/specsteward:init`")]
    found.append(_git(cfg.root))
    found += _checks(cfg)
    found += _permissions(cfg.root)
    found += _duplicate_hooks(cfg.root)
    found += _install(cfg.root)
    found.append(_graph(cfg))
    found += extra or []
    found += _tools()
    found += _remote()
    return found


def format_findings(found: list[Finding]) -> str:
    lines = []
    for item in found:
        fix = f"  -> {item.fix}" if item.fix and item.level != OK else ""
        lines.append(f"{item.level:<4} {item.what}{fix}")
    fails = sum(item.level == FAIL for item in found)
    warns = sum(item.level == WARN for item in found)
    lines.append(f"SDD DOCTOR: {'FAIL' if fails else 'OK'} ({fails} to fix, {warns} warnings)")
    return "\n".join(lines)
