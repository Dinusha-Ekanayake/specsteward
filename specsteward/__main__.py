"""CLI: ``sdd <command>``. In a project, Claude runs it as ``bash .sdd/bin/sdd <command>``."""

import argparse
import io
import json
import os
import sys
from pathlib import Path
from typing import NoReturn

from specsteward import config as cfg_mod
from specsteward import drift, graph, phone, prereqs, trust, wizard
from specsteward import scope as scope_mod
from specsteward import state as run_state
from specsteward.analyze import analyze
from specsteward.analyze import format_findings as format_analysis
from specsteward.brief import write_brief
from specsteward.check import format_report, run_all, signature
from specsteward.dashboard import status_line, write_dashboard
from specsteward.doctor import format_findings, run_doctor
from specsteward.hooks import (
    SDD,
    format_file,
    guard_decision,
    session_context,
    stop_decision,
    write_shim,
)
from specsteward.init import enable_statusline, init_project
from specsteward.presets import PRESETS
from specsteward.remote import remote_status
from specsteward.shell_guard import shell_guard_decision
from specsteward.tasks import parse_tasks

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
# Outside bin/: Claude Code puts a plugin's bin/ on PATH, and claude.ai and Cowork refuse
# plugins that have one.
LAUNCHER = PLUGIN_ROOT / "scripts" / "sdd.sh"
ENTRY = PLUGIN_ROOT / "scripts" / "sdd_entry.py"
HOOKS = (
    "hook-stop",
    "hook-format",
    "hook-guard",
    "hook-shell-guard",
    "hook-subagent-stop",
    "hook-session-start",
    "hook-bind",
)


def version() -> str:
    """The plugin version, from its manifest."""
    manifest = PLUGIN_ROOT / ".claude-plugin" / "plugin.json"
    try:
        return str(json.loads(manifest.read_text(encoding="utf-8"))["version"])
    except (OSError, ValueError, KeyError):
        return "unknown"


def _start_dir() -> Path:
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path.cwd())


COMMAND_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "setup",
        (
            ("init", "set up sdd.toml, templates and permissions"),
            ("trust", "trust the current sdd.toml (keep behind an ask rule)"),
            ("doctor", "check the setup and say how to fix problems"),
            ("install", "install a prerequisite (bun, for phone control)"),
            ("phone", "set up phone control in one step, or lock the bot to you"),
        ),
    ),
    (
        "specs",
        (
            ("analyze", "check a spec, plan and tasks before approval"),
            ("approve", "mark a spec approved (keep behind an ask rule)"),
            ("worktree", "a parallel git worktree for one spec"),
        ),
    ),
    (
        "running a spec",
        (
            ("start", "start a run for an approved spec"),
            ("next", "the next task group to run, or halted / done"),
            ("halt", "pause the run with a reason for the user"),
            ("resume", "clear a halt after the user dealt with it (asks for permission)"),
            ("signoff", "sign off a human task waiting at [?] (asks you)"),
            ("claim", "continue an active run from this Claude Code session"),
            ("stop", "end the run"),
        ),
    ),
    (
        "views and status",
        (
            ("status", "progress, halt reason, assumptions, tokens"),
            ("dashboard", "write .sdd/dashboard.html and print its path"),
            ("statusline", "one line for Claude Code's status line"),
            ("graph", "code graph: show its status, or sync it now"),
            ("remote", "phone control status (Telegram or Discord)"),
        ),
    ),
    (
        "used by the agents during a run",
        (
            ("agent", "count a subagent run"),
            ("brief", "write .sdd/briefs/<first-id>.md"),
            ("check", "run the quality gate"),
            ("scope", "list changes outside the files the approved plan names"),
            ("review", "record the reviewer's verdict for the group"),
            ("done", "tick a task (needs a passing gate and review)"),
            ("assume", "log an assumption the implementer made"),
            ("converge", "count a converge pass and print the next task id"),
        ),
    ),
)


def _help(name: str) -> str:
    """The one-line help of a public command, from COMMAND_GROUPS."""
    for _, commands in COMMAND_GROUPS:
        for command, text in commands:
            if command == name:
                return text
    raise KeyError(name)


def _grouped_commands() -> str:
    """The command list for `sdd --help`, under one heading per group."""
    width = max(len(name) for _, commands in COMMAND_GROUPS for name, _ in commands)
    blocks = []
    for heading, commands in COMMAND_GROUPS:
        lines = [f"  {name:<{width}}  {text}" for name, text in commands]
        blocks.append("\n".join([f"{heading}:", *lines]))
    return "\n\n".join(blocks)


class _Parser(argparse.ArgumentParser):
    """Plain messages for unknown commands, without the internal hook names."""

    def format_help(self) -> str:
        text = super().format_help()
        if self.prog != "sdd":  # only the top level lists the grouped commands
            return text
        text = text.replace("[--version]\n", "[--version] <command> ...\n", 1)
        head, sep, tail = text.partition("\noptions:")
        return f"{head}\n{_grouped_commands()}\n{sep}{tail}"

    def error(self, message: str) -> NoReturn:
        if "invalid choice" in message and "<command>" in message:
            given = message.split("invalid choice: ", 1)[1].split(" ", 1)[0]
            message = f"unknown command {given}; run `sdd --help` to see the commands"
        super().error(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="sdd",
        usage="sdd [-h] [--version] <command> ...",
        description="specsteward: spec-driven runs for Claude Code, with human gates.",
        epilog="In a project, run it as `bash .sdd/bin/sdd <command>`. Docs: "
        "https://github.com/Dinusha-Ekanayake/specsteward",
    )
    parser.add_argument("--version", action="version", version=f"specsteward {version()}")
    sub = parser.add_subparsers(
        dest="command", prog="sdd", required=True, metavar="<command>", help=argparse.SUPPRESS
    )
    init = sub.add_parser("init", help=_help("init"), description=_help("init"))
    init.add_argument(
        "--preset", choices=PRESETS, default="auto", help="stack to set up (default: detect it)"
    )
    init.add_argument("--specs-dir", default="specs", help="folder for the specs (default: specs)")
    init.add_argument("--force", action="store_true", help="overwrite sdd.toml and the templates")
    init.add_argument("--statusline", action="store_true", help="show the run in the status line")
    check = sub.add_parser("check", help=_help("check"), description=_help("check"))
    check.add_argument(
        "--task", help="the orchestrator's gate: count failures for this task's caps"
    )
    brief = sub.add_parser("brief", help=_help("brief"), description=_help("brief"))
    brief.add_argument("tasks", nargs="+", help="task ids of one group, for example T-003-01")
    start = sub.add_parser("start", help=_help("start"), description=_help("start"))
    start.add_argument("spec", help="three-digit spec number, for example 003")
    start.add_argument("--no-branch-check", action="store_true", help="start on any branch")
    approve = sub.add_parser("approve", help=_help("approve"), description=_help("approve"))
    approve.add_argument("spec", help="three-digit spec number, for example 003")
    approve.add_argument(
        "--via",
        default="terminal",
        help="where the approval came from: terminal, telegram, discord",
    )
    approve.add_argument("--start", action="store_true", help="start the run right after")
    approve.add_argument(
        "--no-branch-check", action="store_true", help="with --start: start on any branch"
    )
    approve.add_argument("--anyway", action="store_true", help="approve despite CRITICAL findings")
    worktree = sub.add_parser("worktree", help=_help("worktree"), description=_help("worktree"))
    worktree.add_argument("spec", help="three-digit spec number, for example 003")
    sub.add_parser("converge", help=_help("converge"), description=_help("converge"))
    analyze_cmd = sub.add_parser("analyze", help=_help("analyze"), description=_help("analyze"))
    analyze_cmd.add_argument("spec", help="three-digit spec number, for example 003")
    agent = sub.add_parser("agent", help=_help("agent"), description=_help("agent"))
    agent.add_argument("role", choices=("planner", "implementer", "reviewer", "converger", "clerk"))
    agent.add_argument("--fresh", action="store_true", help="a new implementer was started")
    sub.add_parser("scope", help=_help("scope"), description=_help("scope"))
    review = sub.add_parser("review", help=_help("review"), description=_help("review"))
    review.add_argument("task", help="task id, for example T-003-01")
    review.add_argument("verdict", choices=("clean", "changes"))
    done = sub.add_parser("done", help=_help("done"), description=_help("done"))
    done.add_argument("task", help="task id, for example T-003-01")
    done.add_argument("--human", action="store_true", help="mark [?]: needs sign-off")
    done.add_argument("--waive-red", metavar="REASON", help="no failing test applies; say why")
    assume = sub.add_parser("assume", help=_help("assume"), description=_help("assume"))
    assume.add_argument("task", help="task id, for example T-003-01")
    assume.add_argument("text", help="the assumption, in one sentence")
    halt = sub.add_parser("halt", help=_help("halt"), description=_help("halt"))
    halt.add_argument("reason", help="why the run stops, and what the user must decide")
    status = sub.add_parser("status", help=_help("status"), description=_help("status"))
    status.add_argument("--json", action="store_true", help="machine-readable output")
    signoff = sub.add_parser("signoff", help=_help("signoff"), description=_help("signoff"))
    signoff.add_argument("task", help="task id, for example T-003-01")
    sub.add_parser("claim", help=_help("claim"), description=_help("claim"))
    sub.add_parser("trust", help=_help("trust"), description=_help("trust"))
    graph_cmd = sub.add_parser("graph", help=_help("graph"), description=_help("graph"))
    graph_cmd.add_argument("action", choices=("status", "sync"))
    sub.add_parser("statusline", help=_help("statusline"), description=_help("statusline"))
    sub.add_parser("dashboard", help=_help("dashboard"), description=_help("dashboard"))
    phone_cmd = sub.add_parser("phone", help=_help("phone"), description=_help("phone"))
    phone_cmd.add_argument("action", choices=("setup", "lock"))
    phone_cmd.add_argument(
        "--channel", choices=("telegram", "discord"), default="telegram", help="which chat app"
    )
    phone_cmd.add_argument(
        "--no-input", action="store_true", help="no questions: only the automatic steps"
    )
    install = sub.add_parser("install", help=_help("install"), description=_help("install"))
    install.add_argument("tool", choices=("bun",))
    doctor = sub.add_parser("doctor", help=_help("doctor"), description=_help("doctor"))
    doctor.add_argument("--json", action="store_true", help="machine-readable output")
    for name in ("next", "resume", "stop", "remote"):
        sub.add_parser(name, help=_help(name), description=_help(name))
    for name in HOOKS:  # called by Claude Code, not by people
        sub.add_parser(name)
    return parser


def _utf8_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):  # Windows consoles default to cp1252
            stream.reconfigure(encoding="utf-8", errors="replace")


GUARD_HOOKS = frozenset(("hook-guard", "hook-shell-guard"))


def _hook(command: str) -> int:
    """Hooks never fail the session: outside an sdd project they do nothing, and an error
    inside one is reported on stderr instead of breaking the user's turn."""
    start = _start_dir()
    try:
        cfg = cfg_mod.load(start)
    except (ValueError, OSError) as error:
        try:
            root = cfg_mod.find_root(start)
        except ValueError:
            return 0  # no sdd.toml here: not an sdd project
        if command not in GUARD_HOOKS:
            return 0
        # sdd.toml exists but is unusable (corrupted, or edited to break the hooks): keep
        # guarding with the defaults instead of going quiet.
        print(f"specsteward {command}: {error}; guarding with the defaults", file=sys.stderr)
        cfg = cfg_mod.Config(root=root)
    try:
        _dispatch_hook(command, cfg)
    except Exception as error:  # noqa: BLE001 - a hook must never take the session down
        print(f"specsteward {command}: {type(error).__name__}: {error}", file=sys.stderr)
    return 0


def _payload() -> dict[str, object]:
    """The hook's JSON input, or {} when there is none (a terminal, or a test).

    The launcher may already have read it (to skip Python for unrelated Bash calls) and
    passes it on in SDD_HOOK_PAYLOAD.
    """
    try:
        passed = os.environ.get("SDD_HOOK_PAYLOAD")
        if passed is not None:
            data = json.loads(passed)
        elif sys.stdin.isatty():
            return {}
        else:
            data = json.load(sys.stdin)
    except (OSError, ValueError, AttributeError):
        return {}
    return data if isinstance(data, dict) else {}


def _dispatch_hook(command: str, cfg: cfg_mod.Config) -> None:
    reads = (
        "hook-stop",
        "hook-subagent-stop",
        "hook-bind",
        "hook-session-start",
        "hook-shell-guard",
    )
    payload = _payload() if command in reads else {}
    session_id = payload.get("session_id")
    if command in ("hook-stop", "hook-subagent-stop") and payload:
        run_state.observe_usage(cfg, payload.get("transcript_path"), session_id)
    if command == "hook-bind":
        run_state.bind_owner(cfg, payload)
    elif command == "hook-stop":
        decision = stop_decision(cfg, session_id)
        if decision:
            print(json.dumps(decision))
    elif command == "hook-format":
        format_file(cfg, json.load(sys.stdin))
    elif command == "hook-subagent-stop":
        agent_type = payload.get("agent_type") or payload.get("subagent_type")
        run_state.observe_agent(cfg, agent_type, session_id)
    elif command == "hook-shell-guard":
        shell = shell_guard_decision(payload)
        if shell:
            print(json.dumps(shell))
    elif command == "hook-guard":
        guard = guard_decision(cfg, json.load(sys.stdin))
        if guard:
            print(json.dumps(guard))
    else:
        write_shim(cfg.root, LAUNCHER)
        text = session_context(cfg, session_id)
        # Only for a trusted sdd.toml: it chooses the graphify version that gets installed.
        if cfg.graphify_auto and trust.config_trusted(cfg.root):
            graph.ensure_ignores(cfg.root)  # now, not mid-run from the background sync
            graph.start_background_sync(cfg.root, ENTRY)
            text += graph.session_hint(cfg.root)
        prereqs.start_background_bun_install(ENTRY)  # only if phone control is set up
        context = {"hookEventName": "SessionStart", "additionalContext": text}
        print(json.dumps({"hookSpecificOutput": context}))


def _graph(cfg: cfg_mod.Config, action: str) -> int:
    if action == "status":
        found = graph.find_graphify()
        print(json.dumps({"graphify": found, "graph": graph.graph_status(cfg.root)}))
        return 0
    try:
        if not trust.config_trusted(cfg.root):
            raise ValueError("sdd.toml is not trusted; review it, then run `sdd trust`")
        lines = graph.sync(cfg.root, cfg.graphify_version)
    finally:
        graph.release_lock(cfg.root)
    print("\n".join(lines))
    return 1 if any(line.startswith(("could not", "`graphify")) for line in lines) else 0


def _phone(action: str, name: str, no_input: bool) -> int:
    channel = phone.channel_named(name)
    if action == "lock":
        print(phone.lock(channel))
        return 0
    if not no_input and wizard.interactive():  # a person at a terminal: guide them through
        return wizard.run(channel)
    ok, lines = phone.setup(channel)
    print("\n".join(lines))
    if ok:
        print("\n".join(["", *phone.next_steps(channel)]))
    return 0 if ok else 1


def _statusline() -> int:
    """Never fails and prints nothing outside a run, so it is safe in any status line."""
    start = _start_dir()
    if not sys.stdin.isatty():
        try:
            payload = json.load(sys.stdin)
            start = Path(payload.get("workspace", {}).get("project_dir") or start)
        except (ValueError, AttributeError, OSError):
            start = _start_dir()  # no usable payload: the status line falls back to the cwd
    try:
        root = cfg_mod.find_root(start)
        state = run_state.load(root)
        tasks = parse_tasks(run_state.tasks_path(root, state)) if state else []
        print(status_line(state, tasks))
    except Exception:  # noqa: BLE001 - a status line must never show a traceback
        pass
    return 0


def _doctor(as_json: bool) -> int:
    found = run_doctor(_start_dir())
    if as_json:
        print(json.dumps([vars(item) for item in found], indent=2))
    else:
        print(format_findings(found))
    return 1 if any(item.level == "FAIL" for item in found) else 0


def _check(cfg: cfg_mod.Config, task: str | None) -> int:
    active = run_state.load(cfg.root)
    if active is not None and active["active"]:
        problem = run_state.config_problem(cfg, active)
        if problem:
            print(f"SDD CHECK: BLOCKED. {problem}")
            return 1
    before = run_state.run_tree(cfg)  # the gate runs project code; see record_pass
    results = run_all(cfg)
    print(format_report(results))
    active = run_state.load(cfg.root)
    if active is not None and active["active"]:
        if cfg.scope_mode != "off":
            for line in scope_mod.notes(cfg, active):
                print(f"SCOPE: {line}")
        for line in drift.notes(cfg, active):
            print(f"DRIFT: {line}")
    if all(r.ok for r in results):
        run_state.record_pass(cfg, before)
        return 0
    if any(not r.ok and run_state.is_test_step(r.name) for r in results):
        run_state.record_red(cfg)
    # Only the orchestrator's gate (`--task`) counts toward the failure caps: the
    # implementer runs the same check while it works, and a failing test is how test-first
    # starts, so its own runs must never halt the run.
    if task:
        state = run_state.record_failure(cfg, task, signature(results))
        if state["halted"]:
            print(f"HALTED: {state['halted']}")
    return 1


def _run_command(args: argparse.Namespace, cfg: cfg_mod.Config) -> int:
    root, command = cfg.root, args.command
    if command == "check":
        return _check(cfg, args.task)
    if command == "worktree":
        target = run_state.create_worktree(cfg, args.spec)
        write_shim(target, LAUNCHER)
        print(f"worktree ready: {target.as_posix()}")
        print(f"open Claude Code there and run /specsteward:run {args.spec}")
        return 0
    if command == "converge":
        print(json.dumps(run_state.converge_round(cfg)))
        return 0
    if command == "next":
        print(json.dumps(run_state.next_step(cfg)))
        return 0
    if command == "brief":
        spec_dir = root / run_state.require(root)["spec_dir"]
        print(write_brief(cfg, spec_dir, args.tasks, SDD).relative_to(root).as_posix())
        return 0
    if command == "analyze":
        spec_dir = cfg.spec_dir(args.spec)
        found = analyze(spec_dir)
        print(format_analysis(spec_dir, found))
        return 1 if any(f.level == "CRITICAL" for f in found) else 0
    if command == "approve":
        found = analyze(cfg.spec_dir(args.spec))
        if any(f.level == "CRITICAL" for f in found) and not args.anyway:
            print(format_analysis(cfg.spec_dir(args.spec), found))
            raise ValueError("resolve the CRITICAL findings first (or pass --anyway)")
        current = run_state.load(root)
        resuming = bool(current and current["active"] and current["spec"] == args.spec)
        if args.start and not resuming and not args.no_branch_check:
            wrong = run_state.branch_problem(cfg, args.spec)
            if wrong:  # before approving, so a refusal leaves nothing half done
                raise ValueError(f"{wrong}; nothing was approved")
        print(f"spec {args.spec}: {run_state.approve(cfg, args.spec, args.via)}")
        if args.start:  # one permission prompt for "approve and go", e.g. from a phone
            if resuming:
                state = run_state.resume(root)  # tasks were added mid-run, e.g. by converge
            else:
                state = run_state.start(cfg, args.spec, not args.no_branch_check)
            print(json.dumps({k: state[k] for k in ("spec", "active", "halted", "agent_runs")}))
        return 0
    if command == "status":
        if args.json:
            print(json.dumps(run_state.load(root)))
        else:
            print(run_state.summary(root))
        return 0
    if command == "graph":
        return _graph(cfg, args.action)
    if command == "dashboard":
        state = run_state.require(root)
        path = write_dashboard(root, state)
        print(path.as_posix() if path else "could not write the dashboard")
        return 0 if path else 1
    if command == "trust":
        print(f"trusted {cfg_mod.CONFIG_FILE} ({trust.trust_config(root)[:12]})")
        run_state.rebaseline_config(root)
        trust.record_guard(root, cfg.guard)
        print("gate config files re-baselined")
        return 0
    if command == "scope":
        run = run_state.require(root)
        lines = scope_mod.notes(cfg, run)
        print("\n".join(lines) if lines else "all changes are inside the approved plan")
        extra = scope_mod.allowed_unplanned(cfg, run)
        if extra:
            print(
                "allowed without being planned (tests, changelog, [scope] allow): "
                + ", ".join(extra)
            )
        for line in drift.notes(cfg, run):
            print(line)
        files = [line for line in lines if line.startswith("outside")]
        symbols = [line for line in lines if line.startswith("unplanned")]
        blocked = (
            (files and cfg.scope_mode == "enforce")
            or (symbols and cfg.symbol_mode == "enforce")
            or drift.problem(cfg, run)
        )
        return 1 if blocked else 0
    if command == "remote":
        print(json.dumps(remote_status(), indent=2))
        return 0
    actions = {
        "start": lambda: run_state.start(cfg, args.spec, not args.no_branch_check),
        "agent": lambda: run_state.record_agent(cfg, args.role, args.fresh),
        "review": lambda: run_state.record_review(cfg, args.task, args.verdict),
        "done": lambda: run_state.complete(cfg, args.task, args.human, args.waive_red),
        "signoff": lambda: run_state.signoff(cfg, args.task),
        "assume": lambda: run_state.record_assumption(root, args.task, args.text),
        "halt": lambda: run_state.halt(root, run_state.require(root), args.reason),
        "resume": lambda: run_state.resume(root),
        "claim": lambda: run_state.claim(root),
        "stop": lambda: run_state.stop(root),
    }
    state = actions[command]()
    if command == "done":
        note = run_state.red_warning(cfg, state, args.task)
        if note:
            print(f"warning: {note}", file=sys.stderr)
    if command == "done" and cfg.scope_mode == "warn":
        for outside in scope_mod.out_of_scope(cfg, state):
            print(f"warning: outside the plan: {outside}", file=sys.stderr)
    if command == "done" and cfg.symbol_mode == "warn":
        for name in scope_mod.unplanned_symbols(cfg, state):
            note = f"warning: unplanned symbol: {name}; name it in the plan or revert"
            print(note, file=sys.stderr)
    if command == "done":
        for line in drift.notes(cfg, state, final=True):
            print(f"warning: {line}", file=sys.stderr)
    print(json.dumps({k: state[k] for k in ("spec", "active", "halted", "agent_runs")}))
    return 0


def main(argv: list[str] | None = None) -> int:
    _utf8_output()
    args = _parser().parse_args(argv)
    if args.command in HOOKS:
        return _hook(args.command)
    try:
        if args.command == "statusline":
            return _statusline()
        if args.command == "phone":
            return _phone(args.action, args.channel, args.no_input)
        if args.command == "install":
            ok, message = prereqs.install_bun()
            print(message)
            return 0 if ok else 1
        if args.command == "doctor":
            return _doctor(args.json)
        if args.command == "init":
            root = _start_dir()
            for line in init_project(root, args.preset, args.specs_dir, args.force):
                print(line)
            if args.statusline:
                print(enable_statusline(root))
            cfg = cfg_mod.load(root)
            trusted = trust.config_trusted(root)
            if cfg.graphify_auto and trusted and graph.start_background_sync(root, ENTRY):
                print(f"building the code graph in the background ({graph.LOG_FILE.as_posix()})")
            write_shim(root, LAUNCHER)
            print(f"next: `{SDD} doctor` to check the setup, then `/specsteward:run plan <topic>`")
            return 0
        return _run_command(args, cfg_mod.load(_start_dir()))
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except (TypeError, KeyError, AttributeError) as error:
        # A value of the wrong kind in sdd.toml or a spec file: say so, without a traceback.
        print(f"error: unexpected value ({type(error).__name__}: {error})", file=sys.stderr)
        print("check sdd.toml against docs/configuration.md, or run `sdd doctor`", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
