import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from specsteward import config, trust
from specsteward import state as run_state
from specsteward.__main__ import main
from specsteward.hooks import format_file, guard_decision, stop_decision, write_shim
from specsteward.init import ASK, DENY, init_project, render_config
from specsteward.remote import telegram_status
from specsteward.tasks import parse_tasks

from .conftest import tasks_file, tick, write_config

ROOT = Path(__file__).resolve().parent.parent

# --- stop hook --------------------------------------------------------------------


def test_stop_hook_inactive_without_run(cfg: config.Config) -> None:
    assert stop_decision(cfg) is None


def test_stop_hook_blocks_then_halts_without_progress(cfg: config.Config) -> None:
    run_state.start(cfg, "009", check_branch=False)
    first = stop_decision(cfg)
    assert first is not None and first["decision"] == "block"
    assert "bash .sdd/bin/sdd next" in first["reason"]
    assert stop_decision(cfg) is None
    assert "no task completed" in run_state.load(cfg.root)["halted"]  # type: ignore[index]


def test_stop_hook_keeps_blocking_while_progress_is_made(cfg: config.Config) -> None:
    run_state.start(cfg, "009", check_branch=False)
    assert stop_decision(cfg) is not None
    tick(cfg, "T-009-02")
    assert stop_decision(cfg) is not None


def test_stop_hook_allows_stop_when_all_done(repo: Path, cfg: config.Config) -> None:
    run_state.start(cfg, "009", check_branch=False)
    for task in parse_tasks(tasks_file(repo)):
        if task.pending:
            tick(cfg, task.id)
    assert stop_decision(cfg) is None


# --- format hook ------------------------------------------------------------------


def _formatter_cfg(repo: Path) -> config.Config:
    script = "import sys,pathlib; p=pathlib.Path(sys.argv[1]); p.write_text(p.read_text().upper())"
    return config.from_dict(repo, {"format": {".py": [[sys.executable, "-c", script, "{file}"]]}})


def test_format_hook_runs_configured_formatter(repo: Path) -> None:
    target = repo / "mod.py"
    target.write_text("x = 1\n")
    format_file(_formatter_cfg(repo), {"tool_input": {"file_path": str(target)}})
    assert target.read_text() == "X = 1\n"


@pytest.mark.parametrize(
    "target", [".sdd/state.json", ".sdd/bin/sdd", ".sdd/../.sdd/approvals.log"]
)
def test_guard_denies_edits_to_run_state(repo: Path, cfg: config.Config, target: str) -> None:
    for payload in (
        {"tool_input": {"file_path": target}, "cwd": str(repo)},
        {"tool_input": {"file_path": str(repo / target)}},
    ):
        decision = guard_decision(cfg, payload)
        assert decision is not None
        assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_guard_denies_edits_to_the_trust_store(cfg: config.Config) -> None:
    payload = {"tool_input": {"file_path": str(trust.store_path())}}
    assert guard_decision(cfg, payload) is not None


@pytest.mark.parametrize("target", ["src/app.py", "sdd.toml", ".sddx/notes.md", ""])
def test_guard_allows_other_files(repo: Path, cfg: config.Config, target: str) -> None:
    assert guard_decision(cfg, {"tool_input": {"file_path": target}, "cwd": str(repo)}) is None
    assert guard_decision(cfg, {}) is None


def test_cli_guard_hook_prints_the_decision(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    payload = json.dumps({"tool_input": {"file_path": str(repo / ".sdd" / "state.json")}})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    assert main(["hook-guard"]) == 0
    assert '"deny"' in capsys.readouterr().out


def test_format_hook_does_nothing_for_an_untrusted_config(repo: Path) -> None:
    target = repo / "mod.py"
    target.write_text("x = 1\n")
    (repo / "sdd.toml").write_text("# changed by a clone or an agent\n")
    format_file(_formatter_cfg(repo), {"tool_input": {"file_path": str(target)}})
    assert target.read_text() == "x = 1\n"


def test_format_hook_ignores_other_suffixes_and_missing_files(repo: Path) -> None:
    notes = repo / "notes.md"
    notes.write_text("x = 1\n")
    cfg = _formatter_cfg(repo)
    format_file(cfg, {"tool_input": {"file_path": str(notes)}})
    format_file(cfg, {"tool_input": {"file_path": str(repo / "gone.py")}})
    format_file(cfg, {})
    assert notes.read_text() == "x = 1\n"


# --- init -------------------------------------------------------------------------


def test_init_creates_everything_once(tmp_path: Path) -> None:
    first = init_project(tmp_path, "python")
    assert "wrote sdd.toml (python project; checks: tests; trusted)" in first
    assert trust.config_trusted(tmp_path)
    assert (tmp_path / "specs" / "_template" / "tasks.md").exists()
    assert ".sdd/" in (tmp_path / ".gitignore").read_text()
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert set(ASK) <= set(settings["permissions"]["ask"])
    assert set(DENY) <= set(settings["permissions"]["deny"])
    second = init_project(tmp_path, "python")
    assert second == ["kept existing sdd.toml"]  # still trusted, so no hint
    config.load(tmp_path)  # the written preset is valid


def test_init_does_not_trust_an_existing_config(tmp_path: Path) -> None:
    (tmp_path / "sdd.toml").write_text("[project]\n")
    lines = init_project(tmp_path, "python")
    assert "kept existing sdd.toml" in lines and not trust.config_trusted(tmp_path)
    assert any("sdd trust" in line for line in lines)


def test_cli_trust_records_the_config(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    (repo / "sdd.toml").write_text("[brief]\ngraphify = false\n# edited\n")
    assert not trust.config_trusted(repo)
    assert main(["trust"]) == 0
    assert "trusted sdd.toml" in capsys.readouterr().out
    assert trust.config_trusted(repo)


def test_init_keeps_existing_settings(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"permissions": {"allow": ["Bash(ls:*)"]}, "model": "x"}))
    init_project(tmp_path, "generic")
    merged = json.loads(settings.read_text())
    assert merged["model"] == "x" and "Bash(ls:*)" in merged["permissions"]["allow"]


@pytest.mark.parametrize("preset", ["auto", "python", "node", "go", "rust", "generic"])
def test_every_preset_is_valid_toml(tmp_path: Path, preset: str) -> None:
    (tmp_path / "sdd.toml").write_text(render_config(tmp_path, preset), encoding="utf-8")
    config.load(tmp_path)


def test_init_writes_the_chosen_specs_dir(tmp_path: Path) -> None:
    init_project(tmp_path, "generic", specs_dir="docs/specs")
    assert config.load(tmp_path).specs_dir == "docs/specs"
    assert (tmp_path / "docs" / "specs" / "_template" / "spec.md").exists()


def test_unknown_preset_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="preset"):
        render_config(tmp_path, "cobol")


# --- remote -------------------------------------------------------------------------


def test_remote_not_paired(tmp_path: Path) -> None:
    status = telegram_status(tmp_path)
    assert status["ready"] is False and "not paired" in status["problems"][0]


def test_remote_ready_with_single_allowlisted_user(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=x\n")
    (tmp_path / "access.json").write_text(
        json.dumps({"dmPolicy": "allowlist", "allowFrom": ["42"]})
    )
    assert telegram_status(tmp_path) == {
        "channel": "telegram",
        "ready": True,
        "chat_ids": ["42"],
        "problems": [],
        "token_set": True,
    }


def test_remote_flags_unsafe_setups(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    access = {"dmPolicy": "pairing", "allowFrom": ["1", "2"], "groups": {"-100": {}}}
    (tmp_path / "access.json").write_text(json.dumps(access))
    problems = " | ".join(telegram_status(tmp_path)["problems"])
    for expected in ("no bot token", "more than one user", "not 'allowlist'", "in groups"):
        assert expected in problems


# --- CLI ----------------------------------------------------------------------------


def test_cli_round_trip(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    assert main(["start", "009"]) == 2  # tmp_path is not a git repo
    assert main(["start", "009", "--no-branch-check"]) == 0
    assert main(["next"]) == 0
    step = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert step["tasks"] == ["T-009-02", "T-009-03"]
    assert main(["brief", "T-009-02"]) == 0
    assert (repo / ".sdd" / "briefs" / "T-009-02.md").exists()
    for argv in (["agent", "implementer", "--fresh"], ["assume", "T-009-02", "x"]):
        assert main(argv) == 0
    assert main(["done", "T-009-02"]) == 2  # no gate or review yet
    assert main(["check"]) == 0
    assert main(["review", "T-009-02", "clean"]) == 0
    assert main(["done", "T-009-02"]) == 0
    assert main(["status"]) == 0
    assert "1 done" not in capsys.readouterr().out.split("Spec 009:")[0]
    assert main(["halt", "need user"]) == 0
    capsys.readouterr()
    assert main(["hook-stop"]) == 0
    assert capsys.readouterr().out == ""  # halted runs never block a stop
    assert main(["resume"]) == 0
    assert main(["status", "--json"]) == 0
    assert main(["stop"]) == 0
    assert main(["remote"]) == 0


def test_cli_check_records_failures(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(
        repo, f'[[check]]\nname = "t"\ncmd = [{json.dumps(sys.executable)}, "-c", "exit(1)"]\n'
    )
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    assert main(["start", "009", "--no-branch-check"]) == 0
    assert main(["check", "--task", "T-009-02"]) == 1
    assert main(["check", "--task", "T-009-02"]) == 1
    assert "same check failure" in run_state.load(repo)["halted"]  # type: ignore[index]


def test_cli_approve(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    assert main(["approve", "009", "--via", "telegram"]) == 0
    assert (repo / ".sdd" / "approvals.log").exists()


def test_cli_approve_and_start_in_one_prompt(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    assert main(["approve", "009", "--start", "--no-branch-check"]) == 0
    assert run_state.require(repo)["spec"] == "009"


def test_everything_that_resets_or_approves_is_an_ask_rule() -> None:
    gated = {rule.split()[-1].rstrip(":*)") for rule in ASK if ".sdd/bin/sdd" in rule}
    assert gated == {"approve", "trust", "start", "resume", "signoff"}


def test_hooks_are_silent_outside_sdd_projects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    for hook in ("hook-stop", "hook-session-start"):
        assert main([hook]) == 0
    assert capsys.readouterr().out == ""
    assert not (tmp_path / ".sdd").exists()


def test_session_start_writes_shim_and_context(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    assert main(["hook-session-start"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "bash .sdd/bin/sdd" in payload["hookSpecificOutput"]["additionalContext"]
    assert (repo / ".sdd" / "bin" / "sdd").read_text().startswith("#!/usr/bin/env bash")


def test_cli_init(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    assert main(["init", "--preset", "generic"]) == 0
    assert (tmp_path / "sdd.toml").exists() and (tmp_path / ".sdd" / "bin" / "sdd").exists()


def test_cli_errors_return_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    assert main(["status"]) == 2  # no sdd.toml


# --- launcher (end to end through bash) --------------------------------------------------


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_launcher_and_shim_run_the_cli(repo: Path) -> None:
    # Resolve through PATH: on Windows a bare "bash" can hit WSL in System32 instead.
    bash = shutil.which("bash")
    assert bash is not None
    shim = write_shim(repo, ROOT / "scripts" / "sdd.sh")
    proc = subprocess.run(
        [bash, shim.relative_to(repo).as_posix(), "status"],
        cwd=repo,
        capture_output=True,
        text=True,
        env={**os.environ, "SDD_PYTHON": sys.executable},
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "No specsteward run in this project."


def test_hook_errors_never_break_the_session(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    assert main(["start", "009", "--no-branch-check"]) == 0

    def broken(cfg: config.Config, session_id: object = None) -> None:
        raise KeyError("boom")

    monkeypatch.setattr("specsteward.__main__.stop_decision", broken)
    assert main(["hook-stop"]) == 0
    assert "specsteward hook-stop: KeyError" in capsys.readouterr().err


def test_format_hook_ignores_files_outside_the_project(
    repo: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = tmp_path_factory.mktemp("elsewhere") / "mod.py"
    outside.write_text("x = 1\n")
    format_file(_formatter_cfg(repo), {"tool_input": {"file_path": str(outside)}})
    assert outside.read_text() == "x = 1\n"


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_launcher_skips_python_for_hooks_outside_sdd_projects(tmp_path: Path) -> None:
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(tmp_path), "SDD_PYTHON": "no-such-python"}
    proc = subprocess.run(
        [str(shutil.which("bash")), str(ROOT / "scripts" / "sdd.sh"), "hook-format"],
        input="{}",
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0 and proc.stdout == "" and proc.stderr == ""


def test_init_explains_the_permission_rules_in_words(tmp_path: Path) -> None:
    lines = init_project(tmp_path, "generic")
    assert any(line.startswith("permission rules (asks you first): ") for line in lines)
    assert any(line.startswith("permission rules (blocked): Edit(.sdd/**)") for line in lines)
    assert not any(line.startswith("permission deny") for line in lines)


def test_unknown_command_does_not_list_internal_hooks(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["frobnicate"])
    err = capsys.readouterr().err
    assert "unknown command 'frobnicate'; run `sdd --help`" in err and "hook-" not in err


def test_an_empty_gate_fails_and_init_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    assert main(["init"]) == 0
    out = capsys.readouterr().out
    assert "add your test command under [[check]]" in out and "/specsteward:run plan" in out
    assert main(["check"]) == 1
    assert "no [[check]] steps, so the gate would prove nothing" in capsys.readouterr().out


def test_rerunning_init_keeps_relaxed_git_rules_but_restores_gates(tmp_path: Path) -> None:
    init_project(tmp_path, "generic")
    settings = tmp_path / ".claude" / "settings.json"
    data = json.loads(settings.read_text())
    data["permissions"]["ask"] = [r for r in data["permissions"]["ask"] if "git push" not in r]
    data["permissions"]["ask"].remove("Bash(bash .sdd/bin/sdd approve:*)")  # a gate removed
    data["permissions"]["deny"] = []
    settings.write_text(json.dumps(data))
    init_project(tmp_path, "generic")  # e.g. after an upgrade
    after = json.loads(settings.read_text())["permissions"]
    assert "Bash(git push:*)" not in after["ask"]  # the user's choice stays
    assert "Bash(bash .sdd/bin/sdd approve:*)" in after["ask"]  # the gate comes back
    assert set(DENY) <= set(after["deny"])


# --- hooks come from the plugin; 0.3.2's project-level copies are removed -------------


def test_init_removes_legacy_project_hooks_and_keeps_others(repo: Path) -> None:
    from specsteward.init import LEGACY_HOOK_PREFIX, init_project

    legacy = {"type": "command", "command": f"{LEGACY_HOOK_PREFIX}guard", "timeout": 30}
    mine = {"type": "command", "command": "echo mine"}
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir(exist_ok=True)
    settings.write_text(
        json.dumps(
            {"hooks": {"Stop": [{"hooks": [legacy, mine]}], "SessionStart": [{"hooks": [legacy]}]}}
        ),
        encoding="utf-8",
    )
    init_project(repo)
    after = json.loads(settings.read_text(encoding="utf-8"))["hooks"]
    assert after == {"Stop": [{"hooks": [mine]}]}
    init_project(repo)  # idempotent
    assert json.loads(settings.read_text(encoding="utf-8"))["hooks"] == after


def test_init_writes_no_hooks_of_its_own(repo: Path) -> None:
    from specsteward.init import init_project

    init_project(repo)
    settings = json.loads((repo / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert "hooks" not in settings


def _legacy(name: str) -> dict[str, object]:
    from specsteward.init import LEGACY_HOOK_PREFIX

    return {"type": "command", "command": f"{LEGACY_HOOK_PREFIX}{name}", "timeout": 30}


def test_legacy_removal_keeps_other_settings_and_untouched_groups(repo: Path) -> None:
    from specsteward.init import LEGACY_HOOK_PREFIX, init_project

    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir(exist_ok=True)
    matcher_only = {"matcher": "Bash"}
    empty_hooks = {"matcher": "Edit", "hooks": []}
    custom = {"hooks": [{"type": "command", "command": f"{LEGACY_HOOK_PREFIX}mine"}]}
    pre = [matcher_only, empty_hooks, custom, {"hooks": [_legacy("guard")]}]
    settings.write_text(
        json.dumps(
            {
                "model": "sonnet",
                "hooks": {
                    "SessionStart": [{"hooks": [_legacy("session-start")]}],
                    "PreToolUse": pre,
                },
            }
        ),
        encoding="utf-8",
    )
    init_project(repo)
    after = json.loads(settings.read_text(encoding="utf-8"))
    assert after["model"] == "sonnet"
    assert "SessionStart" not in after["hooks"]
    assert after["hooks"]["PreToolUse"] == [matcher_only, empty_hooks, custom]


def test_only_legacy_hooks_leaves_no_hooks_key(repo: Path) -> None:
    from specsteward.init import init_project

    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir(exist_ok=True)
    settings.write_text(
        json.dumps({"hooks": {"Stop": [{"hooks": [_legacy("stop")]}]}, "env": {"A": "1"}}),
        encoding="utf-8",
    )
    done = init_project(repo)
    after = json.loads(settings.read_text(encoding="utf-8"))
    assert "hooks" not in after and after["env"] == {"A": "1"}
    assert any("removed project hooks" in line and "stop" in line for line in done)


@pytest.mark.parametrize(
    "raw",
    ['{"hooks": {"Stop": {"a": 1}}}', '{"hooks": {"Stop": ["x"]}}', "[]", '{"hooks": 5}'],
)
def test_malformed_hook_settings_never_crash_init(repo: Path, raw: str) -> None:
    from specsteward.init import _drop_legacy_project_hooks

    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir(exist_ok=True)
    settings.write_text(raw, encoding="utf-8")
    assert _drop_legacy_project_hooks(settings) == []
    assert settings.read_text(encoding="utf-8") == raw  # left exactly as it was


def test_unparsable_settings_are_left_alone(repo: Path) -> None:
    from specsteward.init import _drop_legacy_project_hooks

    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir(exist_ok=True)
    settings.write_text("{not json", encoding="utf-8")
    assert _drop_legacy_project_hooks(settings) == []
    assert _drop_legacy_project_hooks(repo / "absent.json") == []
