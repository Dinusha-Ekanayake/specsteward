"""Release hygiene, and the review fixes that keep a public install trouble-free."""

import json
import os
import re
import threading
import tomllib
from pathlib import Path

import pytest

from specsteward import config, trust
from specsteward.init import render_config
from specsteward.remote import telegram_status

ROOT = Path(__file__).resolve().parent.parent


def test_versions_agree() -> None:
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    package = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    top = re.search(r"^## (\d+\.\d+\.\d+)", changelog, re.MULTILINE)
    assert top is not None
    marketplace = json.loads(
        (ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )
    listed = {entry["version"] for entry in marketplace["plugins"]}
    assert plugin["version"] == package["project"]["version"] == top.group(1)
    assert listed == {plugin["version"]}
    # the documents a user copies from name the same release
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"specsteward@v{plugin['version']}`" in readme, "README pin example is stale"
    minor = ".".join(plugin["version"].split(".")[:2])
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert f"| {minor}.x | Yes |" in security, "SECURITY.md does not list this release"


def test_generated_config_follows_the_plugins_graphify_pin(tmp_path: Path) -> None:
    (tmp_path / "sdd.toml").write_text(render_config(tmp_path, "generic"), encoding="utf-8")
    assert config.load(tmp_path).graphify_version == config.GRAPHIFY_VERSION


def test_concurrent_writers_do_not_lose_records(repo: Path) -> None:
    errors: list[Exception] = []

    def approve(spec: str) -> None:
        try:
            trust.record_approval(repo, spec, f"hash-{spec}")
        except Exception as error:  # noqa: BLE001 - surfaced by the assert below
            errors.append(error)

    threads = [threading.Thread(target=approve, args=(f"{n:03d}",)) for n in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert all(trust.approved_hash(repo, f"{n:03d}") == f"hash-{n:03d}" for n in range(20))


def test_a_stale_lock_from_a_killed_process_is_cleared(repo: Path) -> None:
    lock = trust.store_path().with_suffix(".lock")
    lock.write_text("")
    old = lock.stat().st_mtime - trust.STALE_LOCK - 1
    os.utime(lock, (old, old))
    trust.record_approval(repo, "010", "abc")
    assert trust.approved_hash(repo, "010") == "abc" and not lock.exists()


def test_a_held_lock_times_out_with_a_clear_error(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(trust, "LOCK_TIMEOUT", 0.1)
    trust.store_path().with_suffix(".lock").write_text("")
    with pytest.raises(ValueError, match="trust store is locked"):
        trust.record_approval(repo, "010", "abc")


@pytest.mark.parametrize(
    ("content", "expected"),
    [("", False), ("TELEGRAM_BOT_TOKEN=\n", False), ("TELEGRAM_BOT_TOKEN=123:abc\n", True)],
)
def test_bot_token_needs_a_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str, expected: bool
) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    (tmp_path / ".env").write_text(content)
    assert telegram_status(tmp_path)["token_set"] is expected


def test_an_unreadable_store_is_never_saved_over_as_empty(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trust.record_approval(repo, "010", "keep-me")
    store = trust.store_path()
    real_read = Path.read_text

    def locked(self: Path, *args: object, **kwargs: object) -> str:
        if self == store:
            raise PermissionError(13, "in use by another process")
        return real_read(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(trust, "RETRY_SECONDS", 0.1)
    monkeypatch.setattr(Path, "read_text", locked)
    with pytest.raises(ValueError, match="cannot read the trust store"):
        trust.record_approval(repo, "011", "new")
    monkeypatch.setattr(Path, "read_text", real_read)
    assert trust.approved_hash(repo, "010") == "keep-me"  # nothing was wiped


def test_a_brief_windows_sharing_clash_is_retried(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_replace = trust.os.replace
    clashes = [PermissionError(13, "sharing violation")] * 3

    def flaky(src: str, dst: str) -> None:
        if clashes:
            raise clashes.pop()
        real_replace(src, dst)

    monkeypatch.setattr(trust.os, "replace", flaky)
    trust.record_approval(repo, "012", "abc")
    assert trust.approved_hash(repo, "012") == "abc" and not clashes


def test_cli_version_matches_the_manifest(capsys: pytest.CaptureFixture[str]) -> None:
    from specsteward.__main__ import main

    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"specsteward {plugin['version']}"


def test_manifests_point_at_the_repository() -> None:
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    assert plugin["repository"].endswith("/specsteward") and plugin["license"] == "MIT"
    assert market["plugins"][0]["version"] == plugin["version"]


@pytest.mark.parametrize(
    "message",
    [
        "same check failure twice",
        "fix attempt cap reached",
        "review round cap reached with open findings",
        "agent run cap reached",
        "token budget reached",
        "no task completed since the last automatic continue",
        "tasks.md changed outside sdd done",
        "converge round cap reached",
        "resolve the CRITICAL findings first",
        "says approved but has no recorded approval",
        "changed after it was approved",
        "no passing sdd check for the code as it is now",
        "no clean review for the code as it is now",
        "files changed while the gate ran",
        "gate config changed during the run",
        "the gate has no [[check]] steps",
        "trust store is locked",
        "Bun is installed but not on PATH",
        "the run is halted (",
        "changes outside the approved plan:",
        "unplanned changes inside planned files:",
        "changed but no test runs them",
        "the coverage report is older than the changes",
        "the coverage report is not the one the last passing gate wrote",
        "exclude settings (.git/info/exclude or core.excludesFile) changed during this",
        "no failing test run was recorded for this group",
        "no reviewer subagent has finished since the last clean verdict and passing gate",
    ],
)
def test_every_halt_and_error_is_explained_in_the_faq(message: str) -> None:
    """The FAQ quotes messages exactly; this keeps it from going stale."""
    source = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "specsteward").glob("*.py"))
    faq = (ROOT / "docs" / "faq.md").read_text(encoding="utf-8").replace("`", "")
    assert message in source.replace("`", ""), f"message no longer in code: {message}"
    assert message in faq, f"FAQ does not explain: {message}"


def test_the_old_plugin_name_migrates_to_the_new_one() -> None:
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    assert market["renames"] == {"sdd-pilot": "specsteward"}
    assert market["plugins"][0]["name"] == "specsteward"
