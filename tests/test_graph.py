import os
import shlex
import subprocess
from pathlib import Path

import pytest

from specsteward import graph
from specsteward.__main__ import main

from .conftest import write_config

REPORT = "# Graph Report\n- Built from commit: `{commit}`\n"


class FakeRunner:
    """Stands in for subprocess: records commands, and can 'install' or 'build'."""

    def __init__(self, root: Path, install_ok: bool = True, build_ok: bool = True) -> None:
        self.root, self.install_ok, self.build_ok = root, install_ok, build_ok
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], cwd: Path, timeout: int) -> int:
        self.calls.append(cmd)
        if cmd[-2:] == ["update", "."]:
            if self.build_ok:
                _write_graph(self.root, "abc")
            return 0 if self.build_ok else 1
        return 0 if self.install_ok else 1


def _write_graph(root: Path, commit: str) -> None:
    out = root / graph.OUT_DIR
    out.mkdir(exist_ok=True)
    (out / "graph.json").write_text("{}")
    (out / "GRAPH_REPORT.md").write_text(REPORT.format(commit=commit))


@pytest.fixture
def no_graphify(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(graph, "find_graphify", lambda: None)


@pytest.fixture
def has_graphify(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(graph, "find_graphify", lambda: "graphify")


def test_status_missing_fresh_and_stale(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert graph.graph_status(repo) == "missing"
    _write_graph(repo, "abc")
    monkeypatch.setattr(graph, "_head", lambda root: "abc123")
    assert graph.graph_status(repo) == "fresh"
    monkeypatch.setattr(graph, "_head", lambda root: "def456")
    assert graph.graph_status(repo) == "stale"
    monkeypatch.setattr(graph, "_head", lambda root: "")  # not a git repo: trust the build
    assert graph.graph_status(repo) == "fresh"


def test_sync_installs_the_pinned_version_then_builds(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    found: list[str | None] = [None]
    monkeypatch.setattr(graph, "find_graphify", lambda: found[0])
    runner = FakeRunner(repo)

    def install_then_find(cmd: list[str], cwd: Path, timeout: int) -> int:
        found[0] = "graphify"
        return runner(cmd, cwd, timeout)

    lines = graph.sync(repo, "1.2.3", install_then_find)
    assert any("graphifyy==1.2.3" in cmd for cmd in runner.calls)
    assert "installed graphifyy==1.2.3" in lines and lines[-1] == "graph built"
    assert (repo / ".graphifyignore").read_text().startswith("# Written by specsteward")
    assert "graphify-out/" in (repo / ".gitignore").read_text()


def test_sync_reports_a_failed_install(repo: Path, no_graphify: None) -> None:
    lines = graph.sync(repo, "1.2.3", FakeRunner(repo, install_ok=False))
    assert lines[-1].startswith("could not install graphifyy==1.2.3")


def test_sync_skips_a_fresh_graph_and_reports_build_failures(
    repo: Path, has_graphify: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(graph, "_head", lambda root: "")
    assert graph.sync(repo, run=FakeRunner(repo, build_ok=False))[-1].startswith("`graphify")
    _write_graph(repo, "abc")
    runner = FakeRunner(repo)
    assert graph.sync(repo, run=runner)[-1] == "graph is up to date"
    assert runner.calls == []


def test_ignores_are_written_once_and_kept(repo: Path) -> None:
    (repo / ".graphifyignore").write_text("mine\n")
    (repo / ".gitignore").write_text("graphify-out\n")
    assert graph.ensure_ignores(repo) == []
    assert (repo / ".graphifyignore").read_text() == "mine\n"


def test_install_order_prefers_isolated_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        graph.shutil, "which", lambda name: name if name in ("uv", "pipx") else None
    )
    commands = graph.install_commands("1.0")
    assert [cmd[0] for cmd in commands[:2]] == ["uv", "pipx"]
    assert commands[2][1:5] == ["-I", "-m", "pip", "install"]  # isolated mode


def test_find_graphify_looks_in_the_user_bin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(graph.shutil, "which", lambda name: None)
    monkeypatch.setattr(graph.Path, "home", lambda: tmp_path)
    assert graph.find_graphify() is None
    exe = tmp_path / ".local" / "bin" / "graphify"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    assert graph.find_graphify() == str(exe)


def test_background_sync_is_single_and_detached(
    repo: Path, has_graphify: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_start = graph._real_start  # type: ignore[attr-defined]  # saved by conftest
    started: list[dict[str, object]] = []
    monkeypatch.setattr(graph.subprocess, "Popen", lambda *a, **kw: started.append(kw))
    assert real_start(repo, Path("entry.py")) is True
    assert real_start(repo, Path("entry.py")) is False  # locked: one build at a time
    assert started[0]["stdin"] == subprocess.DEVNULL and len(started) == 1
    graph.release_lock(repo)
    _write_graph(repo, "abc")
    monkeypatch.setattr(graph, "_head", lambda root: "")
    assert real_start(repo, Path("entry.py")) is False  # nothing to do


def test_stale_lock_does_not_block(repo: Path) -> None:
    lock = repo / graph.LOCK_FILE
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("1")
    old = lock.stat().st_mtime - graph.LOCK_SECONDS - 1
    os.utime(lock, (old, old))
    assert not graph._locked(repo)


def test_session_start_syncs_and_points_agents_at_the_graph(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    no_background_graph: list[Path],
) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    assert main(["hook-session-start"]) == 0
    assert no_background_graph == [repo]
    assert "being built in the background" in capsys.readouterr().out
    _write_graph(repo, "abc")
    assert main(["hook-session-start"]) == 0
    assert "graphify query" in capsys.readouterr().out


def test_auto_off_means_no_sync(
    repo: Path, monkeypatch: pytest.MonkeyPatch, no_background_graph: list[Path]
) -> None:
    cfg = write_config(repo, "[graphify]\nauto = false\n")
    assert cfg.graphify_auto is False
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    assert main(["hook-session-start"]) == 0
    assert no_background_graph == []


def test_cli_graph_status_and_sync(
    repo: Path,
    has_graphify: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    assert main(["graph", "status"]) == 0
    assert '"graph": "missing"' in capsys.readouterr().out
    monkeypatch.setattr(graph, "_run", FakeRunner(repo))
    real_sync = graph.sync
    monkeypatch.setattr(graph, "sync", lambda root, version: real_sync(root, version, graph._run))
    assert main(["graph", "sync"]) == 0
    assert "graph built" in capsys.readouterr().out
    assert not (repo / graph.LOCK_FILE).exists()


def test_cli_init_starts_a_background_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    no_background_graph: list[Path],
) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    assert main(["init"]) == 0
    assert no_background_graph == [tmp_path]
    assert "building the code graph in the background" in capsys.readouterr().out


def test_installers_run_outside_the_repository(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(graph.Path, "home", lambda: home)
    monkeypatch.setattr(graph, "find_graphify", lambda: None)
    seen: list[Path] = []

    def record(cmd: list[str], cwd: Path, timeout: int) -> int:
        seen.append(cwd)
        return 1

    graph.install("1.0", record)
    assert seen and all(cwd == home for cwd in seen)


def test_untrusted_config_never_syncs_or_installs(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    no_background_graph: list[Path],
) -> None:
    (repo / "sdd.toml").write_text('[graphify]\nversion = "6.6.6"\n')  # e.g. a fresh clone
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    assert main(["hook-session-start"]) == 0
    assert no_background_graph == []
    assert main(["graph", "sync"]) == 2
    assert "not trusted" in capsys.readouterr().err


def test_installs_use_the_exact_dependency_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    from specsteward.config import GRAPHIFY_VERSION

    monkeypatch.setattr(
        graph.shutil, "which", lambda name: name if name in ("uv", "pipx") else None
    )
    uv, pipx, pip = graph.install_commands(GRAPHIFY_VERSION)
    assert uv[-2:] == ["-c", str(graph.CONSTRAINTS)]
    assert pipx[-1] == f"--pip-args=-c {shlex.quote(graph.CONSTRAINTS.as_posix())}"
    assert pip[-2:] == ["-c", str(graph.CONSTRAINTS)]
    other = graph.install_commands("9.9.9")  # a version chosen in sdd.toml: no lock
    assert all("-c" not in cmd for cmd in other)


def test_the_lock_pins_graphify_and_every_dependency_exactly() -> None:
    from specsteward.config import GRAPHIFY_VERSION

    lines = [
        line.strip()
        for line in graph.CONSTRAINTS.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert f"graphifyy=={GRAPHIFY_VERSION}" in lines
    assert len(lines) > 10 and all("==" in line for line in lines)
