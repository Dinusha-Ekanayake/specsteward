from pathlib import Path

import pytest

from specsteward import config
from specsteward.tasks import DONE, HUMAN, TODO, next_group, parse_tasks, set_status

from .conftest import tasks_file

# --- config -------------------------------------------------------------------


def test_defaults_without_sections(cfg: config.Config) -> None:
    assert cfg.specs_dir == "specs"
    assert cfg.models == {"low": "sonnet", "high": "opus", "human": "opus"}
    assert config.from_dict(cfg.root, {}).check == ()  # no steps unless configured
    assert cfg.limits.max_fix_attempts == 3


def test_load_finds_config_in_parent(repo: Path) -> None:
    nested = repo / "src" / "deep"
    nested.mkdir(parents=True)
    assert config.load(nested).root == repo


def test_missing_config_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="/specsteward:init"):
        config.load(tmp_path)


def test_check_steps_and_format_parsed(repo: Path) -> None:
    raw = {
        "check": [{"name": "tests", "cmd": ["pytest", "-q"], "keep": "FAILED"}],
        "format": {".py": [["ruff", "format", "{file}"]]},
        "models": {"low": "haiku"},
    }
    cfg = config.from_dict(repo, raw)
    assert cfg.check[0] == config.CheckStep("tests", ("pytest", "-q"), "FAILED")
    assert cfg.format_cmds[".py"] == (("ruff", "format", "{file}"),)
    assert cfg.models["low"] == "haiku" and cfg.models["high"] == "opus"


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        ({"check": [{"name": "x", "cmd": "pytest -q"}]}, "non-empty list"),
        ({"check": [{"name": "x", "cmd": []}]}, "non-empty list"),
        ({"check": [{"name": "x", "cmd": ["x"], "timeout": 0}]}, "timeout"),
        ({"check": [{"name": "x", "cmd": ["x"], "keep": "("}]}, "missing \\)"),
        ({"check": [{"cmd": ["x"]}]}, "name and a cmd"),
        ({"models": {"medium": "opus"}}, "keys must be"),
        ({"limits": {"max_fix_attempts": 0}}, "whole number, 1 or more"),
        ({"limits": {"max_foo": 3}}, "has no key 'max_foo'"),
        ({"limits": {"max_tokens": "many"}}, "max_tokens must be a whole number"),
        ({"scope": {"allow": 3}}, "a string or a list of strings"),
        ({"groups": {"max_size": "a"}}, "max_size must be a whole number"),
        ({"groups": {"max_size": 0}}, "max_size must be a whole number"),
        ({"project": {"branch_prefix": "feature/{x}-"}}, "placeholder {spec}"),
        ({"check": [{"name": "t", "cmd": ["x"], "timeout": "soon"}]}, "whole number of seconds"),
    ],
)
def test_invalid_config_rejected(repo: Path, raw: dict, match: str) -> None:  # type: ignore[type-arg]
    with pytest.raises(ValueError, match=match):
        config.from_dict(repo, raw)


def test_bad_banned_regex_fails_at_load(repo: Path) -> None:
    with pytest.raises(Exception, match="unterminated|missing"):
        config.from_dict(repo, {"wording": {"banned": ["(oops"]}})


def test_status_and_approval(cfg: config.Config) -> None:
    assert cfg.is_approved("- **Status:** Approved (2026-01-01)")
    assert not cfg.is_approved("- **Status:** Draft")
    assert not cfg.is_approved("no status line")


def test_spec_dir_requires_exactly_one_match(cfg: config.Config) -> None:
    assert cfg.spec_dir("009").name == "009-demo"
    with pytest.raises(ValueError, match="there is no spec 777"):
        cfg.spec_dir("777")


# --- tasks.md -----------------------------------------------------------------


def test_parse_tasks_reads_risk_and_status(repo: Path) -> None:
    tasks = parse_tasks(tasks_file(repo))
    assert [t.risk for t in tasks] == ["low", "high", "high", "high", "low", "human"]
    assert tasks[0].status == DONE and tasks[1].pending


def test_parse_tasks_defaults_risk_without_column(tmp_path: Path) -> None:
    path = tmp_path / "tasks.md"
    path.write_text(
        "| ID | Task | Test | Status |\n|---|---|---|---|\n| T-001-01 | a | b | [ ] |\n"
    )
    assert parse_tasks(path)[0].risk == "low"


@pytest.mark.parametrize(
    ("row", "match"),
    [
        ("| T-001-01 | a | b | huge | [ ] |", "risk"),
        ("| T-001-01 | a | b | low | done |", "status"),
    ],
)
def test_parse_tasks_rejects_bad_cells(tmp_path: Path, row: str, match: str) -> None:
    path = tmp_path / "tasks.md"
    path.write_text(f"| ID | Task | Test | Risk | Status |\n{row}\n")
    with pytest.raises(ValueError, match=match):
        parse_tasks(path)


def test_parse_tasks_rejects_empty_table(tmp_path: Path) -> None:
    path = tmp_path / "tasks.md"
    path.write_text("# nothing\n")
    with pytest.raises(ValueError, match="no tasks"):
        parse_tasks(path)


def test_set_status_changes_only_that_row(repo: Path) -> None:
    path = tasks_file(repo)
    set_status(path, "T-009-02", HUMAN)
    statuses = [t.status for t in parse_tasks(path)]
    assert statuses == [DONE, HUMAN, TODO, TODO, TODO, TODO]
    with pytest.raises(ValueError, match="not found"):
        set_status(path, "T-009-99", DONE)
    with pytest.raises(ValueError, match="unknown status"):
        set_status(path, "T-009-02", "[!]")


def test_next_group_caps_high_risk_and_isolates_human(repo: Path) -> None:
    tasks = parse_tasks(tasks_file(repo))
    assert [t.id for t in next_group(tasks)] == ["T-009-02", "T-009-03"]
    assert [t.id for t in next_group(tasks, max_high=3)] == ["T-009-02", "T-009-03", "T-009-04"]
    rest = [t for t in tasks if t.id in ("T-009-01", "T-009-06")]
    assert [t.id for t in next_group(rest)] == ["T-009-06"]
    assert next_group([t for t in tasks if not t.pending]) == []


def test_pipes_inside_backticks_or_escaped_stay_in_the_cell(tmp_path: Path) -> None:
    path = tmp_path / "tasks.md"
    path.write_text(
        "| ID | Task | Test | Risk | Status |\n|---|---|---|---|---|\n"
        "| T-001-01 | parse `a | b` and a \\| c | ok | low | [ ] |\n"
    )
    task = parse_tasks(path)[0]
    assert task.text == "parse `a | b` and a | c" and task.status == "[ ]"


def test_set_status_refuses_when_status_is_not_the_last_column(tmp_path: Path) -> None:
    path = tmp_path / "tasks.md"
    path.write_text(
        "| ID | Task | Test | Status | Risk |\n|---|---|---|---|---|\n"
        "| T-001-01 | x | y | [ ] | low |\n"
    )
    with pytest.raises(ValueError, match="last column"):
        set_status(path, "T-001-01", "[x]")
    assert "[ ]" in path.read_text()


def test_two_folders_for_one_spec_number_are_named(repo: Path, cfg: config.Config) -> None:
    (repo / "specs" / "009-other").mkdir()
    with pytest.raises(ValueError, match="009-demo, 009-other"):
        cfg.spec_dir("009")
