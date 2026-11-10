"""Run state in ``.sdd/state.json``: progress, caps, halts and the approval log."""

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from functools import wraps
from pathlib import Path
from typing import Any, TypeVar

from specsteward import drift, gitsafe, scope, trust, usage
from specsteward.config import CONFIG_FILE, SPEC_NUMBER, Config
from specsteward.dashboard import write_dashboard
from specsteward.graph import ensure_ignores
from specsteward.safefs import ensure_safe
from specsteward.tasks import DONE, HUMAN, TODO, next_group, parse_tasks, set_status
from specsteward.worktree import fingerprint

STATE_DIR = Path(".sdd")
STATE_FILE = STATE_DIR / "state.json"
APPROVALS_FILE = STATE_DIR / "approvals.log"

State = dict[str, Any]


LOCK_TIMEOUT = 10.0
STALE_LOCK = 30.0
_held = threading.local()
F = TypeVar("F", bound=Callable[..., Any])


@contextmanager
def _lock(root: Path) -> Iterator[None]:
    """Serialise read-modify-write of state.json across sessions and hook processes."""
    if getattr(_held, "depth", 0):  # a locked function calling another one
        _held.depth += 1
        try:
            yield
        finally:
            _held.depth -= 1
        return
    path = root / STATE_FILE
    ensure_safe(root, path)
    lock = path.with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + LOCK_TIMEOUT
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except (FileExistsError, PermissionError):  # PermissionError: Windows, file in use
            try:
                if time.time() - lock.stat().st_mtime > STALE_LOCK:
                    lock.unlink(missing_ok=True)  # left behind by a killed process
                    continue
            except OSError:
                continue
            if time.monotonic() > deadline:
                raise ValueError(f"run state is locked ({lock}); try again") from None
            time.sleep(0.05)
    _held.depth = 1
    try:
        yield
    finally:
        _held.depth = 0
        lock.unlink(missing_ok=True)


def locked(func: F) -> F:
    """Run a state-changing function under the state lock (first argument: Config or root)."""

    @wraps(func)
    def wrapper(first: Any, *args: Any, **kwargs: Any) -> Any:
        with _lock(first.root if isinstance(first, Config) else first):
            return func(first, *args, **kwargs)

    return wrapper  # type: ignore[return-value]


def load(root: Path) -> State | None:
    """The run started by ``sdd start`` here, or None.

    State a repository ships (a committed ``.sdd/state.json``) does not count: its run id
    must match the one ``start`` recorded in the trust store, and its fields must be sane,
    because they end up in prompts (the Stop hook's continue reason).
    """
    path = root / STATE_FILE
    if not path.exists():
        return None
    try:
        state: State = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    run_id = state.get("run_id")
    if not run_id or run_id != trust.current_run(root):
        return None
    spec_dir = str(state.get("spec_dir", ""))
    if not SPEC_NUMBER.fullmatch(str(state.get("spec", ""))) or ".." in Path(spec_dir).parts:
        return None
    return state


def save(root: Path, state: State) -> None:
    path = root / STATE_FILE
    ensure_safe(root, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")  # write then rename, so a crash never leaves half a file
    ensure_safe(root, tmp)
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    trust._retrying(lambda: os.replace(tmp, path))
    write_dashboard(root, state)  # the read-only HTML view stays current


def require(root: Path) -> State:
    state = load(root)
    if state is None or not state["active"]:
        raise ValueError(
            "no active specsteward run; approve and start one with `sdd approve <NNN> --start` "
            "(or `sdd start <NNN>` when the spec is already approved)"
        )
    return state


def _not_halted(state: State) -> None:
    """A halted run waits for the user: no verdict, agent run or tick is taken meanwhile."""
    if state.get("halted"):
        raise ValueError(
            f"the run is halted ({state['halted']}); show the user why, and continue only "
            "after they resume it with `sdd resume`"
        )


def tasks_path(root: Path, state: State) -> Path:
    return root / str(state["spec_dir"]) / "tasks.md"


def current_branch(root: Path) -> str:
    """The checked-out branch; `symbolic-ref` also works before the first commit."""
    for args in (["symbolic-ref", "--short", "HEAD"], ["rev-parse", "--abbrev-ref", "HEAD"]):
        proc = subprocess.run(
            [*gitsafe.GIT, *args], cwd=root, capture_output=True, text=True, check=False
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    raise ValueError("not a git repository")


@locked
def approve(cfg: Config, spec: str, via: str) -> str:
    """Mark a spec approved, bind the approval to its content, and log it.

    Projects should put ``sdd approve`` behind an ``ask`` permission rule, so the
    approval is a permission prompt only the human can answer (in the terminal, or
    relayed to their phone by a channel plugin). The sha256 of the approved ``spec.md``
    goes to the trust store outside the repository, so editing the status line by hand,
    or changing the spec after approval, does not count as approval.
    """
    path = cfg.spec_dir(spec) / "spec.md"
    text = path.read_text(encoding="utf-8")
    if not re.search(cfg.status_regex, text):
        raise ValueError(f"{path.name} has no status line matching {cfg.status_regex!r}")
    added = _appended_tasks(cfg, spec, path.parent)  # checks before anything is written
    stamp = f"{cfg.approved_word} ({date.today().isoformat()})"
    new_text = re.sub(cfg.status_regex, lambda m: f"{m.group(1)}{stamp}", text, count=1)
    ensure_safe(cfg.root, path)
    path.write_text(new_text, encoding="utf-8")
    digest = approval_digest(path.parent)
    trust.record_approval(cfg.root, spec, digest)
    if added is not None:
        trust.record_ticks(cfg.root, spec, added)
    entry = {
        "time": datetime.now(UTC).isoformat(timespec="seconds"),
        "spec": spec,
        "sha256": digest,
        "via": via,  # reported by the caller; the permission prompt is the real gate
    }
    log = cfg.root / APPROVALS_FILE
    ensure_safe(cfg.root, log)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
    return stamp


def _appended_tasks(cfg: Config, spec: str, directory: Path) -> dict[str, str] | None:
    """For a spec with a run in progress: the ticks after approving tasks appended to it
    (for example by converge). Existing rows must be unchanged and new rows open.
    None when no run of this spec is in progress."""
    state = load(cfg.root)
    recorded = trust.recorded_ticks(cfg.root, spec)
    if state is None or not state["active"] or state["spec"] != spec or recorded is None:
        return None
    actual = {t.id: t.status for t in parse_tasks(directory / "tasks.md")}
    changed = sorted(i for i in recorded if actual.get(i) != recorded[i])
    if changed:
        raise ValueError(
            f"existing tasks changed during the run ({', '.join(changed)}); restore them first"
        )
    new = {i: s for i, s in actual.items() if i not in recorded}
    if any(status != TODO for status in new.values()):
        raise ValueError("tasks added during a run must be open ([ ])")
    return {**recorded, **new}


def create_worktree(cfg: Config, spec: str) -> Path:
    """A sibling git worktree on the spec's branch, so specs can run in parallel.

    The worktree shares trust and approvals with this checkout; it gets its own run
    state. ``sdd.toml`` and the spec are copied over when they are not committed yet.
    """
    directory = cfg.spec_dir(spec)
    slug = directory.name.split("-", 1)[1]
    is_fix = slug.startswith("fix-")
    prefix = (cfg.fix_branch_prefix if is_fix else cfg.branch_prefix) or "feature/{spec}-"
    branch = prefix.format(spec=spec) + (slug.removeprefix("fix-") if is_fix else slug)
    target = cfg.root.parent / f"{cfg.root.name}-{spec}"
    if target.exists():
        raise ValueError(f"{target} already exists")
    exists = (
        subprocess.run(
            [*gitsafe.GIT, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=cfg.root,
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )
    args = [str(target), branch] if exists else ["-b", branch, str(target)]
    proc = subprocess.run(
        ["git", "worktree", "add", *args], cwd=cfg.root, capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise ValueError(f"git worktree add failed: {proc.stderr.strip()}")
    for rel in (Path("sdd.toml"), directory.relative_to(cfg.root)):
        source, copy = cfg.root / rel, target / rel
        if copy.exists():
            continue
        ensure_safe(target, copy)
        if source.is_dir():
            shutil.copytree(source, copy, symlinks=True)
        else:
            shutil.copy2(source, copy)
    return target


def converge_round(cfg: Config) -> dict[str, Any]:
    """Count a converge pass once every task is ticked; give the next free task id."""
    state = require(cfg.root)
    tasks = parse_tasks(tasks_path(cfg.root, state))
    if any(t.pending for t in tasks):
        raise ValueError("converge runs after every task is ticked; run `sdd next`")
    state["converge_rounds"] = int(state.get("converge_rounds", 0)) + 1
    limit = cfg.limits.max_converge_rounds
    if state["converge_rounds"] > limit:
        halt(cfg.root, state, f"converge round cap reached ({limit}); ship or plan a new spec")
        return {"status": "halted", "reason": state["halted"]}
    save(cfg.root, state)
    last = max(int(t.id.rsplit("-", 1)[1]) for t in tasks)
    return {
        "status": "converge",
        "round": state["converge_rounds"],
        "next_id": f"T-{state['spec']}-{last + 1:02d}",
    }


def approval_digest(directory: Path, crlf: bool = False) -> str:
    """What the user approves: spec.md, plan.md and the task definitions.

    Task statuses are left out, so ticking does not void the approval; ticks are checked
    separately (see ``tick_problem``). Line endings are normalized, so git's CRLF/LF
    conversion does not void it; ``crlf`` gives the digest that versions before 0.2.1
    recorded for files with Windows line endings.
    """
    digest = hashlib.sha256()
    for name in ("spec.md", "plan.md"):
        path = directory / name
        data = trust.normalized(path.read_bytes()) if path.is_file() else b""
        if crlf:
            data = data.replace(b"\n", b"\r\n")
        digest.update(name.encode() + b"\0" + data)
    try:
        rows = parse_tasks(directory / "tasks.md")
    except (OSError, ValueError):
        rows = []
    for task in rows:
        digest.update("\0".join(("task", task.id, task.text, task.test, task.risk)).encode())
    return digest.hexdigest()


def tick_problem(cfg: Config, state: State) -> str | None:
    """Task statuses that changed outside ``sdd done`` (a hand-ticked or deleted row)."""
    recorded = trust.recorded_ticks(cfg.root, state["spec"])
    if recorded is None:
        return "no recorded task statuses for this run; start it with `sdd start`"
    actual = {t.id: t.status for t in parse_tasks(tasks_path(cfg.root, state))}
    changed = sorted(i for i in recorded.keys() | actual.keys() if recorded.get(i) != actual.get(i))
    if changed:
        return f"tasks.md changed outside `sdd done` ({', '.join(changed)}); restore it"
    return None


def integrity_problem(cfg: Config, state: State) -> str | None:
    """Approval and ticks, checked before anything trusts the spec or the task table."""
    directory = cfg.root / state["spec_dir"]
    return (
        approval_problem(cfg, state["spec"], directory)
        or tick_problem(cfg, state)
        or config_problem(cfg, state)
    )


def config_problem(cfg: Config, state: State) -> str | None:
    """sdd.toml is bound to the run: trust remembers older versions too, so swapping one
    back in (a weaker gate, a wider scope) would otherwise look trusted."""
    recorded = state.get("config_hash")
    if recorded is None or trust.file_hash(cfg.root / CONFIG_FILE) == recorded:
        return None
    return (
        f"{CONFIG_FILE} changed since the run started; review it, run `sdd trust` "
        "and then `sdd resume`"
    )


@locked
def rebaseline_config(root: Path) -> None:
    """After `sdd trust`: the reviewed sdd.toml is the one this run now follows."""
    state = load(root)
    if state is None or not state["active"]:
        return
    state["config_hash"] = trust.file_hash(root / CONFIG_FILE)
    save(root, state)


def approval_problem(cfg: Config, spec: str, directory: Path) -> str | None:
    """Why the spec does not count as approved right now, or None when it does."""
    spec_file = directory / "spec.md"
    name = f"{directory.name}/spec.md"
    if not cfg.is_approved(spec_file.read_text(encoding="utf-8")):
        return f"{name} is not approved; get user approval first"
    recorded = trust.approved_hash(cfg.root, spec)
    if recorded is None:
        return f"{name} says approved but has no recorded approval; run `sdd approve {spec}`"
    if recorded not in (approval_digest(directory), approval_digest(directory, crlf=True)):
        return (
            f"{directory.name} (spec, plan or task table) changed after it was approved; "
            "the user must approve it again"
        )
    return None


def branch_problem(cfg: Config, spec: str) -> str | None:
    """Why a run for ``spec`` cannot start on the checked-out branch, or None."""
    prefix = cfg.branch_prefix.format(spec=spec)
    if not prefix:
        return None
    allowed = [p for p in (prefix, cfg.fix_branch_prefix.format(spec=spec)) if p]
    if any(current_branch(cfg.root).startswith(p) for p in allowed):
        return None
    return f"switch to a {' or '.join(p + '*' for p in allowed)} branch first"


@locked
def start(cfg: Config, spec: str, check_branch: bool = True) -> State:
    """Start a fresh run. Refuses while a run is active, so counters are never wiped by accident."""
    current = load(cfg.root)
    if current is not None and current["active"]:
        hint = (
            "continue it with `sdd next` (`sdd claim` from a new session, `sdd resume` if it "
            "is halted)"
            if current["spec"] == spec
            else "run `sdd stop` first"
        )
        raise ValueError(f"a run for spec {current['spec']} is already active; {hint}")
    directory = cfg.spec_dir(spec)
    problem = approval_problem(cfg, spec, directory)
    if problem:
        raise ValueError(problem)
    if check_branch:
        wrong = branch_problem(cfg, spec)
        if wrong:
            raise ValueError(wrong)
    tasks = parse_tasks(directory / "tasks.md")  # fail early on a malformed table
    trust.record_ticks(cfg.root, spec, {t.id: t.status for t in tasks})
    trust.record_guard(cfg.root, cfg.guard)  # baseline for the gate config check
    if cfg.graphify_auto:
        ensure_ignores(cfg.root)  # so a background graph sync cannot change the tree mid-run
    run_id = secrets.token_hex(8)
    trust.record_run(cfg.root, run_id)
    state: State = {
        "run_id": run_id,
        "spec": spec,
        "spec_dir": directory.relative_to(cfg.root).as_posix(),
        "active": True,
        "halted": None,
        "agent_runs": 0,
        "implementer_tasks": 0,
        "last_block_done": -1,
        "observed_agent_runs": 0,
        "started_at": datetime.now(UTC).isoformat(),
        "usage": usage.empty(),
        "gate": _fresh_gate([]),
        "config_hash": trust.file_hash(cfg.root / CONFIG_FILE),
        "scope": scope.snapshot(cfg.root),
        "tasks": {},
    }
    save(cfg.root, state)
    return state


def _fresh_gate(group: list[str]) -> dict[str, Any]:
    """Which group is in flight, and the tree fingerprints its gate and review last saw."""
    return {"group": group, "passed": None, "reviewed": None, "red_seen": False}


def tree(root: Path, state: State) -> str:
    """Fingerprint of the working tree, leaving out the task table that ``done`` ticks."""
    return fingerprint(root, exclude=(f"{state['spec_dir']}/tasks.md",))


def _gate(state: State) -> dict[str, Any]:
    gate: dict[str, Any] = state.setdefault("gate", _fresh_gate([]))
    return gate


def _task(state: State, task_id: str) -> dict[str, Any]:
    entry: dict[str, Any] = state["tasks"].setdefault(
        task_id, {"failures": [], "fix_attempts": 0, "review_rounds": 0, "assumptions": []}
    )
    return entry


@locked
def halt(root: Path, state: State, reason: str) -> State:
    state["halted"] = reason
    save(root, state)
    return state


@locked
def next_step(cfg: Config) -> dict[str, Any]:
    state = require(cfg.root)
    if state["halted"]:
        return {"status": "halted", "reason": state["halted"]}
    problem = integrity_problem(cfg, state)
    if problem:
        halt(cfg.root, state, problem)
        return {"status": "halted", "reason": problem}
    tasks = parse_tasks(tasks_path(cfg.root, state))
    gate = _gate(state)
    # A group stays in flight, with its gate and review results, until all of it is ticked.
    remaining = [t for t in tasks if t.pending and t.id in gate["group"]]
    group = remaining or next_group(tasks, cfg.max_group, cfg.max_group_high)
    if not group:
        return {"status": "done", "spec": state["spec"]}
    risk = group[0].risk
    ids = [t.id for t in group]
    if not remaining:
        state["gate"] = _fresh_gate(ids)
        save(cfg.root, state)
    return {
        "status": "run",
        "tasks": ids,
        "risk": risk,
        "review": risk not in cfg.review_skip,
        "reviewer_model": None if risk in cfg.review_skip else cfg.models[risk],
        "human_signoff": risk == "human",
        "restart_implementer": state["implementer_tasks"] >= cfg.limits.restart_after_tasks,
    }


def agent_count(state: State) -> int:
    """Declared (``sdd agent``) or observed (SubagentStop hook) runs, whichever is higher."""
    return max(int(state["agent_runs"]), int(state.get("observed_agent_runs", 0)))


def _cap_agents(cfg: Config, state: State) -> State:
    if agent_count(state) > cfg.limits.max_agent_runs:
        return halt(cfg.root, state, f"agent run cap reached ({cfg.limits.max_agent_runs})")
    save(cfg.root, state)
    return state


IN_FLIGHT_SECONDS = 2 * 60 * 60  # longer than any one subagent run should take
OUR_AGENT_PREFIXES = ("specsteward:", "sdd-pilot:")


@locked
def record_agent(cfg: Config, role: str, fresh: bool) -> State:
    state = require(cfg.root)
    _not_halted(state)
    state["agent_runs"] += 1
    state["last_agent_at"] = time.time()
    if role == "implementer" and fresh:
        state["implementer_tasks"] = 0
    return _cap_agents(cfg, state)


def ours(agent_type: object) -> bool:
    """A subagent of this plugin. Unknown (older Claude Code sends no type) counts as ours."""
    return not agent_type or str(agent_type).startswith(OUR_AGENT_PREFIXES)


_OWNING_COMMAND = re.compile(
    r"\.sdd/bin/sdd\s+(?:start|resume|claim)\b|\.sdd/bin/sdd\s+approve\b.*--start"
)


def owned_elsewhere(state: State, session_id: object) -> bool:
    """The run belongs to another Claude Code session (unknown sessions are not excluded)."""
    owner = state.get("owner_session")
    return bool(owner and session_id and owner != session_id)


@locked
def bind_owner(cfg: Config, payload: dict[str, Any]) -> State | None:
    """PostToolUse on Bash: the session that starts, resumes or claims the run owns it.

    Only the owner is told to continue, and only its subagents and tokens count, so a
    second session open on the same project (a VS Code panel, a new terminal) cannot
    drive the same run or skew its caps.
    """
    tool_input = payload.get("tool_input") or {}
    command = str(tool_input.get("command", "")) if isinstance(tool_input, dict) else ""
    session_id = payload.get("session_id")
    if not session_id or not _OWNING_COMMAND.search(command):
        return None
    state = load(cfg.root)
    if state is None or not state["active"]:
        return None
    if state.get("owner_session") == session_id:
        return None
    state["hooks_live"] = True  # this hook ran, so SubagentStop is installed too
    state["owner_session"] = str(session_id)
    save(cfg.root, state)
    return state


@locked
def observe_agent(
    cfg: Config, agent_type: object = None, session_id: object = None
) -> State | None:
    """Count a finished subagent from the SubagentStop hook, so the cap is not honour-based.

    Other subagents in the same session (an Explore search, a code review the user asked
    for) are not part of the run and are not counted.
    """
    state = load(cfg.root)
    if state is None or not state["active"] or state["halted"] or not ours(agent_type):
        return None
    if owned_elsewhere(state, session_id):
        return None  # another session's subagent is not part of this run
    state["observed_agent_runs"] = int(state.get("observed_agent_runs", 0)) + 1
    if str(agent_type or "").endswith("reviewer"):
        state["observed_reviews"] = int(state.get("observed_reviews", 0)) + 1
        state["last_reviewer_at"] = time.time()
    return _cap_agents(cfg, state)


def agent_in_flight(state: State) -> bool:
    """A subagent was launched (`sdd agent`) and has not finished yet (SubagentStop).

    Expires after IN_FLIGHT_SECONDS, so a Claude Code without SubagentStop, or a lost
    notification, cannot switch off the no-progress protection for good.
    """
    launched = int(state.get("agent_runs", 0))
    finished = int(state.get("observed_agent_runs", 0))
    started = float(state.get("last_agent_at") or 0)
    return launched > finished and time.time() - started < IN_FLIGHT_SECONDS


def run_tree(cfg: Config) -> str | None:
    """The fingerprint the gate starts from, or None outside a run."""
    state = load(cfg.root)
    if state is None or not state["active"]:
        return None
    return tree(cfg.root, state)


@locked
def observe_usage(cfg: Config, transcript_path: object, session_id: object = None) -> State | None:
    """Add the run's new token usage from the transcripts; halt over ``max_tokens``."""
    state = load(cfg.root)
    if state is None or not state["active"] or state["halted"] or not transcript_path:
        return None
    if owned_elsewhere(state, session_id):
        return None
    since = datetime.fromisoformat(str(state.get("started_at") or datetime.now(UTC).isoformat()))
    state["usage"] = usage.collect(state.get("usage") or usage.empty(), str(transcript_path), since)
    spent = usage.fresh(state["usage"]["totals"])
    budget = cfg.limits.max_tokens
    if budget and spent > budget:
        return halt(
            cfg.root, state, f"token budget reached ({usage.short(spent)} of {usage.short(budget)})"
        )
    save(cfg.root, state)
    return state


@locked
def record_pass(cfg: Config, before: str | None = None) -> State | None:
    """Remember the tree a passing gate saw (only during a run, for the group in flight).

    With ``before`` (the fingerprint taken as the gate started), a tree that changed while
    the gate ran is not recorded: the pass would not describe the code as it is now.
    """
    state = load(cfg.root)
    if state is None or not state["active"] or not _gate(state)["group"]:
        return None
    now = tree(cfg.root, state)
    if before is not None and before != now:
        raise ValueError("files changed while the gate ran; run `sdd check` again")
    _gate(state)["passed"] = now
    _gate(state)["passed_at"] = time.time()
    _gate(state)["coverage"] = drift.report_digest(cfg)
    save(cfg.root, state)
    return state


def current_task(cfg: Config) -> str | None:
    """First task of the group in flight, which failures are recorded against."""
    state = load(cfg.root)
    if state is None or not state["active"]:
        return None
    group = _gate(state)["group"]
    return str(group[0]) if group else None


@locked
def record_red(cfg: Config) -> State | None:
    """A test step of the gate failed while a group is in flight: the "red" of red/green.

    Any `sdd check` counts, including the implementer's own, because a failing test is how
    test-first starts. It is evidence that a failing run happened, not proof that the
    failure was the right one.
    """
    state = load(cfg.root)
    if state is None or not state["active"] or not _gate(state)["group"]:
        return None
    if _gate(state).get("red_seen"):
        return state
    _gate(state)["red_seen"] = True
    save(cfg.root, state)
    return state


RED_WAIVER = "red-first waived: "


def tdd_problem(cfg: Config, state: State, task_id: str, risk: str) -> str | None:
    """Why the group has no red run on record, or None. A human task has no tests to run."""
    if cfg.tdd_mode == "off" or risk == "human" or not has_test_step(cfg):
        return None
    if _gate(state).get("red_seen"):
        return None
    notes = _task(state, task_id)["assumptions"]
    if any(str(note).startswith(RED_WAIVER) for note in notes):
        return None
    return (
        "no failing test run was recorded for this group, so test-first is not shown; "
        "write the failing test, run `sdd check`, then implement. If no test applies, "
        f'say why with `sdd done {task_id} --waive-red "reason"`'
    )


def has_test_step(cfg: Config) -> bool:
    return any(is_test_step(step.name) for step in cfg.check)


def is_test_step(name: str) -> bool:
    return "test" in name.lower()


@locked
def record_failure(cfg: Config, task_id: str, signature: str) -> State:
    state = require(cfg.root)
    entry = _task(state, task_id)
    if signature in entry["failures"]:
        return halt(cfg.root, state, f"{task_id}: same check failure twice ({signature})")
    entry["failures"].append(signature)
    entry["fix_attempts"] += 1
    if entry["fix_attempts"] > cfg.limits.max_fix_attempts:
        return halt(cfg.root, state, f"{task_id}: fix attempt cap reached")
    save(cfg.root, state)
    return state


def _review_witnessed(state: State) -> bool:
    """A clean verdict needs a reviewer subagent the SubagentStop hook saw finish.

    Enforced only once the hook has observed any subagent in this run; where the hook
    does not fire (an older Claude Code) the verdict stays honour-based.
    """
    if not int(state.get("observed_agent_runs", 0)) and not state.get("hooks_live"):
        return True
    passed_at = _gate(state).get("passed_at")
    if passed_at is not None and float(state.get("last_reviewer_at", 0)) <= float(passed_at):
        return False  # the code passed the gate again after the last reviewer finished
    return int(state.get("observed_reviews", 0)) > int(state.get("accepted_reviews", 0))


@locked
def record_review(cfg: Config, task_id: str, verdict: str) -> State:
    if verdict not in ("clean", "changes"):
        raise ValueError("verdict must be 'clean' or 'changes'")
    state = require(cfg.root)
    _not_halted(state)
    _known_task(cfg.root, state, task_id)
    group = _gate(state)["group"]
    if group and task_id not in group:
        raise ValueError(
            f"{task_id} is not in the group in flight ({', '.join(group)}); review that group"
        )
    entry = _task(state, task_id)
    if verdict == "clean" and not _review_witnessed(state):
        raise ValueError(
            "no reviewer subagent has finished since the last clean verdict and passing gate; "
            "run specsteward:reviewer first (a verdict is not taken on trust)"
        )
    entry["review_rounds"] += 1
    if verdict == "clean" and (int(state.get("observed_agent_runs", 0)) or state.get("hooks_live")):
        state["accepted_reviews"] = int(state.get("accepted_reviews", 0)) + 1
    _gate(state)["reviewed"] = tree(cfg.root, state) if verdict == "clean" else None
    if verdict == "changes" and entry["review_rounds"] >= cfg.limits.max_review_rounds:
        return halt(cfg.root, state, f"{task_id}: review round cap reached with open findings")
    save(cfg.root, state)
    return state


@locked
def _known_task(root: Path, state: State, task_id: str) -> None:
    try:
        known = {task.id for task in parse_tasks(tasks_path(root, state))}
    except (OSError, ValueError):
        return  # an unreadable table is reported by the commands that need it
    if task_id not in known:
        raise ValueError(f"there is no task {task_id} in {state['spec_dir']}/tasks.md")


def record_assumption(root: Path, task_id: str, text: str) -> State:
    state = require(root)
    _known_task(root, state, task_id)
    _task(state, task_id)["assumptions"].append(text)
    save(root, state)
    return state


def done_problem(cfg: Config, state: State, task_id: str, risk: str = "high") -> str | None:
    """Why ``task_id`` cannot be ticked yet, or None when it can."""
    gate = _gate(state)
    if task_id not in gate["group"]:
        flying = ", ".join(gate["group"]) or "none"
        return f"{task_id} is not in the group in flight ({flying}); run `sdd next`"
    now = tree(cfg.root, state)
    if gate["passed"] != now:
        return "no passing `sdd check` for the code as it is now; run the gate again"
    if risk not in cfg.review_skip and gate["reviewed"] != now:
        return "no clean review for the code as it is now; review it (again) first"
    problem = scope.problem(cfg, state) or drift.problem(cfg, state)
    if problem:
        return problem
    return tdd_problem(cfg, state, task_id, risk) if cfg.tdd_mode == "enforce" else None


def _task_risk(cfg: Config, state: State, task_id: str) -> str:
    """The task's risk from tasks.md; the strictest one when the table cannot be read."""
    try:
        return {t.id: t.risk for t in parse_tasks(tasks_path(cfg.root, state))}.get(task_id, "high")
    except (OSError, ValueError):
        return "high"


def red_warning(cfg: Config, state: State, task_id: str) -> str | None:
    """Warn mode: the same finding, shown but not blocking."""
    if cfg.tdd_mode != "warn":
        return None
    if tdd_problem(cfg, state, task_id, _task_risk(cfg, state, task_id)) is None:
        return None
    return (
        f"{task_id} is ticked, but no failing test run was recorded for this group, so "
        "test-first is not shown. Next time run the failing test through `sdd check` before "
        'implementing, or say why none applies with `--waive-red "reason"`'
    )


@locked
def complete(cfg: Config, task_id: str, human: bool = False, waive_red: str | None = None) -> State:
    """Tick a task, but only after a passing gate and a clean review of the current tree."""
    state = require(cfg.root)
    _not_halted(state)
    path = tasks_path(cfg.root, state)
    problem = integrity_problem(cfg, state)
    if problem:
        halt(cfg.root, state, problem)
        raise ValueError(problem)
    risk = {t.id: t.risk for t in parse_tasks(path)}.get(task_id, "high")
    if waive_red:
        # Kept in the task's assumptions, so the user sees the reason in the report.
        _task(state, task_id)["assumptions"].append(f"{RED_WAIVER}{waive_red}")
    problem = done_problem(cfg, state, task_id, risk)
    if problem:
        raise ValueError(problem)
    status = HUMAN if human or risk == "human" else DONE
    ensure_safe(cfg.root, path)
    set_status(path, task_id, status)
    ticks = trust.recorded_ticks(cfg.root, state["spec"]) or {}
    trust.record_ticks(cfg.root, state["spec"], {**ticks, task_id: status})
    state["implementer_tasks"] += 1
    save(cfg.root, state)
    return state


@locked
def signoff(cfg: Config, task_id: str) -> State:
    """The user's sign-off on a human task: ``[?]`` becomes ``[x]``.

    Keep it behind an ``ask`` rule: it is the user's judgement, and editing tasks.md by
    hand instead would halt the run as a table changed outside ``sdd done``. Works after
    the run was stopped too: a human task can be judged after the code shipped.
    """
    state = load(cfg.root)
    if state is None:
        raise ValueError("no specsteward run in this project to sign off")
    problem = integrity_problem(cfg, state)
    if problem:
        raise ValueError(problem)
    path = tasks_path(cfg.root, state)
    status = {t.id: t.status for t in parse_tasks(path)}.get(task_id)
    if status != HUMAN:
        raise ValueError(f"{task_id} is not waiting for sign-off (status {status or 'unknown'})")
    ensure_safe(cfg.root, path)
    set_status(path, task_id, DONE)
    ticks = trust.recorded_ticks(cfg.root, state["spec"]) or {}
    trust.record_ticks(cfg.root, state["spec"], {**ticks, task_id: DONE})
    save(cfg.root, state)
    return state


@locked
def claim(root: Path) -> State:
    """Make the calling session the run's owner (recorded by the PostToolUse hook)."""
    return require(root)


@locked
def resume(root: Path) -> State:
    """Clear a halt after the user has dealt with it; open tasks get fresh counters."""
    state = require(root)
    state["halted"] = None
    state["last_block_done"] = -1
    open_ids = {t.id for t in parse_tasks(tasks_path(root, state)) if t.pending}
    for task_id in open_ids & set(state["tasks"]):
        state["tasks"][task_id].update(failures=[], fix_attempts=0, review_rounds=0)
    save(root, state)
    return state


@locked
def stop(root: Path) -> State:
    state = require(root)
    state["active"] = False
    save(root, state)
    return state


def _phase(state: State, open_ids: list[str]) -> str:
    if not state["active"]:
        return "Stopped"
    if state["halted"]:
        return "Halted"
    return "Running" if open_ids else "All tasks done; converge, then ship"


def summary(root: Path) -> str:
    """A few plain lines for a phone screen: progress, halt reason, assumptions."""
    state = load(root)
    if state is None:
        return "No specsteward run in this project."
    tasks = parse_tasks(tasks_path(root, state))
    done = [t.id for t in tasks if t.status == DONE]
    waiting = [t.id for t in tasks if t.status == HUMAN]
    open_ids = [t.id for t in tasks if t.pending]
    lines = [
        f"Spec {state['spec']}: {len(done)} done, {len(waiting)} waiting for you, "
        f"{len(open_ids)} open. {_phase(state, open_ids)}.",
    ]
    if state["halted"]:
        lines.append(f"Halted: {state['halted']}")
    if waiting:
        lines.append(f"Sign-off needed: {', '.join(waiting)}")
    totals = (state.get("usage") or {}).get("totals") or {}
    if any(totals.values()):
        lines.append(f"Used: {usage.describe(totals)}")
    if open_ids:
        lines.append(f"Next: {open_ids[0]}")
    assumptions = [
        f"{task_id}: {text}"
        for task_id, entry in state["tasks"].items()
        for text in entry.get("assumptions", [])
    ]
    if assumptions:
        lines.append("Assumptions:\n- " + "\n- ".join(assumptions[-5:]))
    return "\n".join(lines)
