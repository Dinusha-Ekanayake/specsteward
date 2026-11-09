"""Trust and approval records kept outside the repository.

A cloned repository can ship any ``sdd.toml``, and its ``[[check]]`` and ``[format]``
commands run without a permission prompt. So specsteward only runs them once the user has
trusted that exact file (``sdd trust``, behind an ``ask`` rule). Spec approvals are stored
the same way, bound to the content of ``spec.md``.

The store lives in the user's home (``~/.claude/specsteward/trust.json``, or ``$SDD_HOME``),
not in the project, so agents editing project files cannot forge it.
"""

import functools
import hashlib
import json
import os
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, TypeVar

from specsteward.config import CONFIG_FILE

STORE_NAME = "trust.json"
T = TypeVar("T")


LEGACY_DIR_NAME = "sdd-pilot"  # the project's name before 0.2.0


def user_dir() -> Path:
    """``~/.claude/specsteward``, moved over from the pre-rename folder on first use."""
    current = Path.home() / ".claude" / "specsteward"
    legacy = Path.home() / ".claude" / LEGACY_DIR_NAME
    if not current.exists() and legacy.is_dir() and not legacy.is_symlink():
        try:
            os.replace(legacy, current)
        except OSError:
            return legacy  # in use right now; keep using it rather than lose records
    return current


def store_path() -> Path:
    """The store, outside any project: a ``$SDD_HOME`` inside the project is refused,
    since agents can write there."""
    home = os.environ.get("SDD_HOME")
    if not home:
        return user_dir() / STORE_NAME
    base = Path(home).resolve()
    for project in (os.environ.get("CLAUDE_PROJECT_DIR"), os.getcwd()):
        if project and base.is_relative_to(Path(project).resolve()):
            raise ValueError(f"SDD_HOME ({home}) must be outside the project ({project})")
    return base / STORE_NAME


def normalized(data: bytes) -> bytes:
    """Text with LF line endings: git's CRLF/LF conversion (Windows, or teams mixing
    systems) must not void an approval or a trust record."""
    return data.replace(b"\r\n", b"\n")


def file_hash(path: Path) -> str | None:
    """sha256 of the file with LF line endings, or None when it does not exist."""
    if not path.is_file():
        return None
    return hashlib.sha256(normalized(path.read_bytes())).hexdigest()


def file_hashes(path: Path) -> set[str | None]:
    """Every fingerprint the file may have been recorded under: the current one, and the
    raw-bytes ones that versions before 0.2.1 stored (with LF or CRLF line endings)."""
    if not path.is_file():
        return {None}
    lf = normalized(path.read_bytes())
    return {hashlib.sha256(data).hexdigest() for data in (lf, lf.replace(b"\n", b"\r\n"))}


def _key(root: Path) -> str:
    return root.resolve().as_posix().lower() if os.name == "nt" else root.resolve().as_posix()


MAX_TRUSTED_CONFIGS = 10  # branches (and worktrees) may each carry their own sdd.toml


@functools.lru_cache(maxsize=64)
def repo_root(root: Path) -> Path:
    """The main checkout of the repository ``root`` belongs to, for git worktrees.

    Trust and approvals are kept per repository, so a linked worktree shares what was
    trusted and approved in the main checkout. Run state stays per worktree.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return root
    common = Path(proc.stdout.strip()) if proc.returncode == 0 and proc.stdout.strip() else None
    if common is None:
        return root
    return common.parent if common.name == ".git" else common


RETRY_SECONDS = 3.0


def _retrying(action: Callable[[], T]) -> T:
    """Windows refuses to open or replace a file another process has open (or is deleting)
    with PermissionError, where POSIX would not; such clashes clear within milliseconds."""
    deadline = time.monotonic() + RETRY_SECONDS
    while True:
        try:
            return action()
        except PermissionError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.02)


def _keep_corrupt_copy(path: Path, text: str) -> None:
    """The store reads as empty (every approval is asked again); keep what was there."""
    with suppress(OSError):  # best effort: the fail-closed reset still applies
        path.with_name(path.name + ".corrupt").write_text(text, encoding="utf-8")


def _load() -> dict[str, Any]:
    """The store; empty only when it does not exist yet or is not valid JSON.

    A file that exists but cannot be read raises instead of reading as empty, because a
    transaction would then save the empty store over every record.
    """
    path = store_path()
    try:
        text = _retrying(lambda: path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"projects": {}}
    except OSError as error:
        raise ValueError(f"cannot read the trust store {path}: {error}") from error
    try:
        data = json.loads(text)
    except ValueError:
        _keep_corrupt_copy(path, text)
        return {"projects": {}}
    if not isinstance(data, dict) or not isinstance(data.get("projects", {}), dict):
        return {"projects": {}}
    data.setdefault("projects", {})
    return data


def _save(data: dict[str, Any]) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _retrying(lambda: os.replace(tmp, path))


LOCK_TIMEOUT = 10.0
STALE_LOCK = 30.0


@contextmanager
def _transaction() -> Iterator[dict[str, Any]]:
    """Load, let the caller change, and save the store under a lock file, so two sessions
    (or a session and a background job) never overwrite each other's records."""
    lock = store_path().with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + LOCK_TIMEOUT
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except (FileExistsError, PermissionError):  # Windows
            try:
                if time.time() - lock.stat().st_mtime > STALE_LOCK:
                    lock.unlink(missing_ok=True)  # left behind by a killed process
                    continue
            except OSError:
                continue
            if time.monotonic() > deadline:
                raise ValueError(f"trust store is locked ({lock}); try again") from None
            time.sleep(0.05)
    try:
        data = _load()
        yield data
        _save(data)
    finally:
        lock.unlink(missing_ok=True)


def _project(data: dict[str, Any], root: Path) -> dict[str, Any]:
    entry: dict[str, Any] = data["projects"].setdefault(_key(root), {})
    return entry


def _read(root: Path) -> dict[str, Any]:
    entry: dict[str, Any] = _load()["projects"].get(_key(root), {})
    return entry


def _trusted_configs(entry: dict[str, Any]) -> list[str]:
    configs = [str(c) for c in entry.get("configs", [])]
    legacy = entry.get("config")  # 0.2.0 development builds kept a single hash
    return [*configs, str(legacy)] if legacy and str(legacy) not in configs else configs


def is_known(root: Path) -> bool:
    """True when this repository already has a record (trusted file, approval or run)."""
    return bool(_read(repo_root(root)))


def trust_config(root: Path) -> str:
    """Record the current ``sdd.toml`` as trusted (for the whole repository); return its hash."""
    digest = file_hash(root / CONFIG_FILE)
    if digest is None:
        raise ValueError(f"no {CONFIG_FILE} in {root}")
    with _transaction() as data:
        entry = _project(data, repo_root(root))
        configs = [c for c in _trusted_configs(entry) if c != digest]
        entry["configs"] = [*configs, digest][-MAX_TRUSTED_CONFIGS:]
        entry.pop("config", None)
    return digest


def config_trusted(root: Path) -> bool:
    digest = file_hash(root / CONFIG_FILE)
    trusted = _trusted_configs(_read(repo_root(root)))
    return digest is not None and any(h in trusted for h in file_hashes(root / CONFIG_FILE))


def record_approval(root: Path, spec: str, digest: str) -> None:
    with _transaction() as data:
        _project(data, repo_root(root)).setdefault("approvals", {})[spec] = digest


def approved_hash(root: Path, spec: str) -> str | None:
    value = _read(repo_root(root)).get("approvals", {}).get(spec)
    return str(value) if value else None


def record_run(root: Path, run_id: str) -> None:
    """The run ``sdd start`` began; state.json without this id is not a run."""
    with _transaction() as data:
        _project(data, root)["run"] = run_id


def current_run(root: Path) -> str | None:
    value = _read(root).get("run")
    return str(value) if value else None


def record_ticks(root: Path, spec: str, statuses: dict[str, str]) -> None:
    """The task statuses only ``sdd start`` and ``sdd done`` may set."""
    with _transaction() as data:
        _project(data, root).setdefault("ticks", {})[spec] = statuses


def recorded_ticks(root: Path, spec: str) -> dict[str, str] | None:
    value = _read(root).get("ticks", {}).get(spec)
    return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else None


def record_guard(root: Path, files: tuple[str, ...]) -> None:
    """Remember the gate config files as they are now (missing files included)."""
    with _transaction() as data:
        _project(data, root)["guard"] = {rel: file_hash(root / rel) for rel in files}


def changed_guard(root: Path, files: tuple[str, ...]) -> list[str]:
    """Gate config files that differ from the recorded baseline."""
    baseline = _read(root).get("guard")
    if baseline is None:
        return []
    return [rel for rel in files if baseline.get(rel) not in file_hashes(root / rel)]
