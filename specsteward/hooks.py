"""Claude Code hooks. All of them do nothing in a project without ``sdd.toml``."""

import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

from specsteward import state as run_state
from specsteward import trust
from specsteward.check import resolve_cmd, without_bot_tokens
from specsteward.config import Config
from specsteward.remote import CHANNELS, state_dir
from specsteward.safefs import ensure_safe
from specsteward.tasks import parse_tasks

SHIM = Path(".sdd") / "bin" / "sdd"
SDD = f"bash {SHIM.as_posix()}"
CONTINUE_REASON = (
    "SDD run for spec {spec}: {open} task(s) still open. Run `{sdd} next` and continue "
    'the /specsteward:run loop. If you need the user, run `{sdd} halt "<reason>"` first, '
    "then ask."
)
SESSION_CONTEXT = (
    "specsteward is active in this project. Run its CLI as `{sdd} <command>` from the "
    "repository root (no `cd` prefix). `{sdd} status` shows the current run."
)


def stop_decision(cfg: Config, session_id: object = None) -> dict[str, str] | None:
    """Block the stop while tasks remain, but only if there was progress since the last block.

    Progress is a ticked task or a finished subagent (its report needs handling). No progress
    since the last automatic continue halts the run, so it can never loop forever. While a
    subagent is still working, the session may stop and wait: its completion wakes it, and
    that wait is neither blocked nor counted as a stop without progress.
    """
    state = run_state.load(cfg.root)
    if state is None or not state["active"] or state["halted"]:
        return None
    if run_state.owned_elsewhere(state, session_id):
        return None  # never drive a run from a session that does not own it
    problem = run_state.integrity_problem(cfg, state)
    if problem:  # never ask Claude to continue on a table it may have edited itself
        run_state.halt(cfg.root, state, problem)
        return None
    tasks = parse_tasks(run_state.tasks_path(cfg.root, state))
    open_count = sum(t.pending for t in tasks)
    if open_count == 0:
        return None
    if run_state.agent_in_flight(state):
        return None
    done = len(tasks) - open_count
    finished = int(state.get("observed_agent_runs", 0))
    progressed = done > state["last_block_done"] or finished > state.get("last_block_agents", 0)
    if not progressed:
        run_state.halt(cfg.root, state, "no task completed since the last automatic continue")
        return None
    state["last_block_done"] = done
    state["last_block_agents"] = finished
    run_state.save(cfg.root, state)
    reason = CONTINUE_REASON.format(spec=state["spec"], open=open_count, sdd=SDD)
    return {"decision": "block", "reason": reason}


GUARD_REASON = (
    "specsteward: {path} holds run state, the trust store or the permission rules that make "
    "the gates. It is not edited with file tools, so gates and caps cannot be edited around. "
    "Use `{sdd} <command>`; the user can change Claude settings with /config or by hand."
)
CLAUDE_SETTINGS = (Path(".claude") / "settings.json", Path(".claude") / "settings.local.json")


def _edited_path(payload: dict[str, Any]) -> str:
    tool_input = payload.get("tool_input") or {}
    return str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")


def _inside(path: Path, directory: Path) -> bool:
    try:
        return path.is_relative_to(directory.resolve())
    except (OSError, ValueError):
        return False


def _same(a: Path, b: Path) -> bool:
    """Same file or folder by identity, so UNC, 8.3 or other aliases of a path match too."""
    try:
        return a.exists() and b.exists() and os.path.samefile(a, b)
    except OSError:
        return False


def _protected(path: Path, directories: tuple[Path, ...], files: tuple[Path, ...]) -> bool:
    if any(_inside(path, d) for d in directories) or path in {f.resolve() for f in files}:
        return True
    if any(_same(path, f) for f in files):
        return True
    return any(_same(parent, d) for parent in (path, *path.parents) for d in directories)


def _access_list_decision(path: Path, raw: str) -> dict[str, Any] | None:
    """Ask (never deny) before file tools edit a phone channel's allowlist.

    ``access.json`` says whose messages may answer permission prompts, so an agent that
    adds an account to it could approve its own gates. The official channel plugin's own
    skills edit it too, which is why this asks instead of blocking.
    """
    for channel in CHANNELS:
        target = state_dir(channel) / "access.json"
        if _same(path, target) or path == target.resolve():  # also when it does not exist yet
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "ask",
                    "permissionDecisionReason": (
                        f"specsteward: {raw} lists who may answer permission prompts from "
                        "your phone. Only allow this edit if you asked for it."
                    ),
                }
            }
    return None


def guard_decision(cfg: Config, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Deny file-tool edits to ``.sdd/``, the trust store and Claude settings (PreToolUse)."""
    raw = _edited_path(payload)
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = Path(str(payload.get("cwd") or cfg.root)) / path
    path = path.resolve()
    directories = (cfg.root / SHIM.parent.parent, trust.store_path().parent)
    files = tuple(cfg.root / rel for rel in CLAUDE_SETTINGS)
    if not _protected(path, directories, files):
        return _access_list_decision(path, raw)
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": GUARD_REASON.format(path=raw, sdd=SDD),
        }
    }


def format_file(cfg: Config, payload: dict[str, Any]) -> None:
    """Run the configured formatters on an edited file. Never fails the edit.

    Does nothing until the user has trusted this exact ``sdd.toml``: the commands come
    from the repository, and this hook runs without a permission prompt.
    """
    if not trust.config_trusted(cfg.root):
        return
    file_path = str(payload.get("tool_input", {}).get("file_path", ""))
    path = Path(file_path)
    if not file_path or not path.is_file() or not _inside(path.resolve(), cfg.root):
        return  # never run project formatters on files outside the project
    for cmd in cfg.format_cmds.get(path.suffix, ()):
        args = [part.replace("{file}", file_path) for part in cmd]
        try:
            subprocess.run(
                resolve_cmd(tuple(args), cfg.root),
                cwd=cfg.root,
                capture_output=True,
                check=False,
                timeout=60,
                env=without_bot_tokens(),
            )
        except (OSError, subprocess.TimeoutExpired):
            continue


def write_shim(root: Path, launcher: Path) -> Path:
    """``.sdd/bin/sdd`` points at this plugin version, so permission rules stay stable."""
    shim = root / SHIM
    ensure_safe(root, shim)
    shim.parent.mkdir(parents=True, exist_ok=True)
    target = shlex.quote(launcher.as_posix())
    shim.write_text(
        f'#!/usr/bin/env bash\nexec bash {target} "$@"\n', encoding="utf-8", newline="\n"
    )
    return shim


OTHER_SESSION = (
    " A specsteward run for spec {spec} belongs to another Claude Code session. Do not "
    "continue it unless the user asks; to take it over here, run `{sdd} claim`."
)


def session_context(cfg: Config | None = None, session_id: object = None) -> str:
    text = SESSION_CONTEXT.format(sdd=SDD)
    state = run_state.load(cfg.root) if cfg is not None else None
    if state and state["active"] and run_state.owned_elsewhere(state, session_id):
        text += OTHER_SESSION.format(spec=state["spec"], sdd=SDD)
    return text
