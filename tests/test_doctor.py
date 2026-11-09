import json
import sys
from pathlib import Path

import pytest

from specsteward.__main__ import main
from specsteward.doctor import FAIL, OK, WARN, Finding, format_findings, run_doctor
from specsteward.init import init_project

from .conftest import write_config


@pytest.fixture(autouse=True)
def isolated_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path_factory.mktemp("home")
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home))
    monkeypatch.setenv("TELEGRAM_STATE_DIR", str(home / "telegram"))


def _levels(found: list[Finding]) -> dict[str, str]:
    return {item.what: item.level for item in found}


def test_without_config_doctor_points_to_init(tmp_path: Path) -> None:
    found = run_doctor(tmp_path)
    assert found[-1].level == FAIL and "init" in found[-1].fix


def _with_a_check(root: Path) -> None:
    write_config(root, f'[[check]]\nname = "tests"\ncmd = [{json.dumps(sys.executable)}]\n')


def test_fresh_init_is_healthy_once_it_has_a_check(tmp_path: Path) -> None:
    init_project(tmp_path, "generic")
    levels = _levels(run_doctor(tmp_path))
    assert levels["the gate has no [[check]] steps, so it proves nothing"] == FAIL
    _with_a_check(tmp_path)
    levels = _levels(run_doctor(tmp_path))
    assert FAIL not in levels.values()
    assert levels["approve, trust, start and resume prompt you"] == OK


def test_a_brand_new_repository_is_recognised(tmp_path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    init_project(tmp_path, "generic")
    assert _levels(run_doctor(tmp_path))["git branch main"] == OK  # no commits yet


def test_a_missing_tool_is_reported_once(repo: Path) -> None:
    steps = "".join(
        f'[[check]]\nname = "{name}"\ncmd = ["no-such-tool-xyz"]\n' for name in ("lint", "tests")
    )
    write_config(repo, steps)
    fails = [f.what for f in run_doctor(repo) if f.level == FAIL and "not found" in f.what]
    assert fails == ["no-such-tool-xyz not found (needed by 'lint', 'tests')"]


def test_untrusted_config_and_missing_tools_fail(repo: Path) -> None:
    (repo / "sdd.toml").write_text('[[check]]\nname = "t"\ncmd = ["no-such-tool-xyz"]\n')
    assert _levels(run_doctor(repo))["sdd.toml is not trusted"] == FAIL
    write_config(repo, '[[check]]\nname = "t"\ncmd = ["no-such-tool-xyz"]\n')
    found = run_doctor(repo)
    assert any(f.level == FAIL and "no-such-tool-xyz" in f.what for f in found)


def test_missing_gates_and_bypass_mode_fail(repo: Path) -> None:
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"permissions": {"defaultMode": "bypassPermissions"}}))
    found = run_doctor(repo)
    whats = {f.what for f in found if f.level == FAIL}
    assert "defaultMode is bypassPermissions" in whats
    assert any(what.startswith("sdd gates do not prompt") for what in whats)


def test_relaxed_git_rules_only_warn(tmp_path: Path) -> None:
    init_project(tmp_path, "generic")
    settings = tmp_path / ".claude" / "settings.json"
    data = json.loads(settings.read_text())
    data["permissions"]["ask"].remove("Bash(git commit:*)")
    settings.write_text(json.dumps(data))
    _with_a_check(tmp_path)
    found = run_doctor(tmp_path)
    assert any(f.level == WARN and "git commit" in f.what for f in found)
    assert FAIL not in {f.level for f in found}


def test_check_steps_report_the_resolved_executable(repo: Path) -> None:
    write_config(repo, f'[[check]]\nname = "t"\ncmd = [{json.dumps(sys.executable)}]\n')
    assert _levels(run_doctor(repo))[f"check 't' runs {Path(sys.executable).name}"] == OK


def test_format_and_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    text = format_findings([Finding(OK, "a", "unused"), Finding(WARN, "b", "do x")])
    assert text.splitlines() == [
        "ok   a",
        "warn b  -> do x",
        "SDD DOCTOR: OK (0 to fix, 1 warnings)",
    ]
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    assert main(["doctor"]) == 1  # no sdd.toml
    init_project(tmp_path, "generic")
    _with_a_check(tmp_path)
    capsys.readouterr()
    assert main(["doctor", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["level"] == OK


def test_doctor_recommends_the_sandbox(tmp_path: Path) -> None:
    init_project(tmp_path, "generic")
    assert any("sandbox is off" in f.what and f.level == WARN for f in run_doctor(tmp_path))
    settings = tmp_path / ".claude" / "settings.json"
    data = json.loads(settings.read_text())
    settings.write_text(json.dumps({**data, "sandbox": {"enabled": True}}))
    assert not any("sandbox" in f.what for f in run_doctor(tmp_path))


def test_doctor_flags_env_overrides_in_settings(tmp_path: Path) -> None:
    init_project(tmp_path, "generic")
    settings = tmp_path / ".claude" / "settings.json"
    data = json.loads(settings.read_text())
    settings.write_text(json.dumps({**data, "env": {"SDD_HOME": "/tmp/x"}}))
    assert any("SDD_HOME is set" in f.what for f in run_doctor(tmp_path))


def test_missing_deny_rules_are_named(tmp_path: Path) -> None:
    init_project(tmp_path, "generic")
    settings = tmp_path / ".claude" / "settings.json"
    data = json.loads(settings.read_text())
    data["permissions"]["deny"] = ["Edit(.sdd/**)"]
    settings.write_text(json.dumps(data))
    whats = [f.what for f in run_doctor(tmp_path) if f.what.startswith("deny rules missing")]
    assert len(whats) == 1 and "Edit(.claude/settings.json)" in whats[0]
    assert "Edit(.sdd/**)" not in whats[0]


def test_doctor_warns_when_hooks_run_twice(tmp_path: Path) -> None:
    from specsteward.init import LEGACY_HOOK_PREFIX

    init_project(tmp_path, "generic")
    assert not any("runs twice" in item.what for item in run_doctor(tmp_path))
    settings = tmp_path / ".claude" / "settings.json"
    data = json.loads(settings.read_text(encoding="utf-8"))
    hook = {"type": "command", "command": f"{LEGACY_HOOK_PREFIX}stop"}
    data["hooks"] = {"Stop": [{"hooks": [hook]}]}
    settings.write_text(json.dumps(data), encoding="utf-8")
    found = [item for item in run_doctor(tmp_path) if "runs twice" in item.what]
    assert len(found) == 1 and found[0].level == WARN and "init" in found[0].fix
