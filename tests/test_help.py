"""The grouped `sdd --help`: display-only, complete against the parser, no hook commands."""

import argparse

import pytest

from specsteward import __main__ as cli

HEADINGS = (
    "setup",
    "specs",
    "running a spec",
    "views and status",
    "used by the agents during a run",
)

MEMBERS = {
    "setup": ("init", "trust", "doctor", "install", "phone"),
    "specs": ("analyze", "approve", "worktree"),
    "running a spec": ("start", "next", "halt", "resume", "signoff", "claim", "stop"),
    "views and status": ("status", "dashboard", "statusline", "graph", "remote"),
    "used by the agents during a run": (
        "agent",
        "brief",
        "check",
        "scope",
        "review",
        "done",
        "assume",
        "converge",
    ),
}

# The help texts as they were before the grouping.
FROZEN_HELP = {
    "init": "set up sdd.toml, templates and permissions",
    "check": "run the quality gate",
    "scope": "list changes outside the files the approved plan names",
    "brief": "write .sdd/briefs/<first-id>.md",
    "start": "start a run for an approved spec",
    "approve": "mark a spec approved (keep behind an ask rule)",
    "worktree": "a parallel git worktree for one spec",
    "converge": "count a converge pass and print the next task id",
    "analyze": "check a spec, plan and tasks before approval",
    "agent": "count a subagent run",
    "review": "record the reviewer's verdict for the group",
    "done": "tick a task (needs a passing gate and review)",
    "assume": "log an assumption the implementer made",
    "halt": "pause the run with a reason for the user",
    "status": "progress, halt reason, assumptions, tokens",
    "trust": "trust the current sdd.toml (keep behind an ask rule)",
    "graph": "code graph: show its status, or sync it now",
    "statusline": "one line for Claude Code's status line",
    "dashboard": "write .sdd/dashboard.html and print its path",
    "install": "install a prerequisite (bun, for phone control)",
    "phone": "set up phone control in one step, or lock the bot to you",
    "doctor": "check the setup and say how to fix problems",
    "next": "the next task group to run, or halted / done",
    "resume": "clear a halt after the user dealt with it (asks for permission)",
    "stop": "end the run",
    # added on main after spec 001 was approved (sessions and human sign-off)
    "signoff": "sign off a human task waiting at [?] (asks you)",
    "claim": "continue an active run from this Claude Code session",
    "remote": "phone control status (Telegram or Discord)",
}


def _table() -> dict[str, tuple[str, str]]:
    return {
        name: (heading, text) for heading, commands in cli.COMMAND_GROUPS for name, text in commands
    }


def _choices(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    raise AssertionError("no subparsers")


def _public(parser: argparse.ArgumentParser) -> set[str]:
    return set(_choices(parser)) - set(cli.HOOKS)


def _problems(parser: argparse.ArgumentParser) -> list[str]:
    """What is wrong with COMMAND_GROUPS against this parser (empty when complete)."""
    names = [name for _, commands in cli.COMMAND_GROUPS for name, _ in commands]
    out = []
    if len(names) != len(set(names)):
        out.append("duplicate")
    if any(name.startswith("hook-") for name in names):
        out.append("hook in a group")
    out += [f"no group: {n}" for n in sorted(_public(parser) - set(names))]
    out += [f"missing command: {n}" for n in sorted(set(names) - _public(parser))]
    return out


def _help(capsys: pytest.CaptureFixture[str]) -> str:
    with pytest.raises(SystemExit) as stop:
        cli.main(["--help"])
    assert stop.value.code == 0
    return capsys.readouterr().out


def test_help_texts_are_unchanged_and_groups_match_the_spec() -> None:
    table = _table()
    assert {n: t for n, (_, t) in table.items()} == FROZEN_HELP
    assert [h for h, _ in cli.COMMAND_GROUPS] == list(HEADINGS)
    for heading, names in MEMBERS.items():
        assert {n for n, (h, _) in table.items() if h == heading} == set(names)


def test_per_command_help_still_comes_from_the_table() -> None:
    for name, sub in _choices(cli._parser()).items():
        if name in FROZEN_HELP:
            assert sub.format_usage().startswith("usage: sdd " + name)
    for action in cli._parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            helps = {a.dest: a.help for a in action._choices_actions}
            assert helps == FROZEN_HELP


def test_groups_cover_exactly_the_public_commands() -> None:
    assert _problems(cli._parser()) == []


def test_completeness_fails_for_an_ungrouped_or_missing_command() -> None:
    parser = cli._parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    sub.add_parser("surprise", help="new")
    assert "no group: surprise" in _problems(parser)
    groups = cli.COMMAND_GROUPS
    try:
        cli.COMMAND_GROUPS = (*groups, ("extra", (("ghost", "x"), ("init", "dup"))))
        problems = _problems(cli._parser())
    finally:
        cli.COMMAND_GROUPS = groups
    assert "missing command: ghost" in problems
    assert "duplicate" in problems


def test_grouped_help_lists_headings_in_order_with_their_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    lines = _help(capsys).splitlines()
    positions = [
        next(i for i, ln in enumerate(lines) if ln.strip().rstrip(":") == h) for h in HEADINGS
    ]
    assert positions == sorted(positions)
    bounds = [*positions, len(lines)]
    for heading, start, end in zip(HEADINGS, bounds, bounds[1:], strict=False):
        block = lines[start + 1 : end]
        for name in MEMBERS[heading]:
            matching = [ln for ln in block if ln.split()[:1] == [name]]
            assert len(matching) == 1, name
            assert matching[0].endswith(FROZEN_HELP[name])


def test_help_hides_hooks_and_the_flat_list_and_keeps_the_rest(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = _help(capsys)
    assert "hook-" not in out
    assert "{" not in out
    assert out.count("run the quality gate") == 1
    assert out.startswith("usage: sdd [-h] [--version] <command> ...")
    assert "specsteward: spec-driven runs" in out
    assert "--version" in out
    assert "-h, --help" in out
    assert "bash .sdd/bin/sdd <command>" in out
    assert "github.com/Dinusha-Ekanayake/specsteward" in out


def test_narrow_terminal_keeps_the_usage_line_intact(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "20")
    out = _help(capsys)
    assert out.startswith("usage: sdd [-h] [--version] <command> ...\n")
    assert "\n  -h, --help" in out


@pytest.mark.parametrize(
    ("command", "options"),
    [
        ("init", ("--preset", "--specs-dir", "--force", "--statusline")),
        ("approve", ("--via", "--start", "--no-branch-check", "--anyway")),
    ],
)
def test_per_command_help_still_lists_its_options(
    command: str,
    options: tuple[str, ...],
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as stop:
        cli.main([command, "--help"])
    assert stop.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith(f"usage: sdd {command}")
    for option in options:
        assert option in out
