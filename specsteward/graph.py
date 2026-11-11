"""Keep a graphify knowledge graph of the project, so agents query it instead of reading files.

On by default (``[graphify] auto = true``): the SessionStart hook and ``sdd init`` start a
background sync that installs graphify when it is missing (a pinned version, through
``uv tool``, ``pipx`` or ``pip --user``), writes ``.graphifyignore``, and builds or refreshes
``graphify-out/``. A fresh clone on another machine gets its graph on the first session.
Building the code graph is local and costs no model tokens.
"""

import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from specsteward import gitsafe
from specsteward.config import GRAPHIFY_VERSION as DEFAULT_VERSION
from specsteward.remote import env_without_tokens
from specsteward.safefs import ensure_safe

PACKAGE = "graphifyy"
OUT_DIR = "graphify-out"
LOG_FILE = Path(".sdd") / "graphify.log"
LOCK_FILE = Path(".sdd") / "graphify.lock"
LOCK_SECONDS = 30 * 60
BUILD_TIMEOUT = 30 * 60
INSTALL_TIMEOUT = 10 * 60
IGNORE_FILE = ".graphifyignore"
# Never index run state, environments, build output or raw data.
IGNORE_DEFAULTS = """\
# Written by specsteward. graphify skips these; add your own data folders.
.sdd/
.git/
.venv/
venv/
node_modules/
dist/
build/
target/
coverage/
htmlcov/
*.min.js
*.map
*.lock
*.csv
*.tsv
*.parquet
*.feather
*.h5
*.hdf5
*.pkl
*.pt
*.onnx
*.safetensors
*.zip
*.gz
data/
datasets/
models/
"""
_BUILT_FROM = re.compile(r"Built from commit: `([0-9a-f]+)`")

Runner = Callable[[list[str], Path, int], int]


def _run(cmd: list[str], cwd: Path, timeout: int) -> int:
    # PYTHONSAFEPATH keeps a Python child from importing modules out of its working
    # directory (a repository could ship its own `pip.py`).
    env = {**env_without_tokens(), "PYTHONSAFEPATH": "1"}
    try:
        return subprocess.run(
            cmd, cwd=cwd, capture_output=True, check=False, timeout=timeout, env=env
        ).returncode
    except (OSError, subprocess.TimeoutExpired):
        return 1


def find_graphify() -> str | None:
    """On PATH, or in the folder ``uv tool`` and ``pipx`` install to (often not on PATH yet)."""
    found = shutil.which("graphify")
    if found:
        return found
    for name in ("graphify.exe", "graphify"):
        candidate = Path.home() / ".local" / "bin" / name
        if candidate.is_file():
            return str(candidate)
    return None


CONSTRAINTS = Path(__file__).resolve().parent / "graphify-constraints.txt"


def install_commands(version: str) -> list[list[str]]:
    """Each installer gets the pinned version and every dependency at an exact version.

    The constraints apply to the version they were generated for; a version chosen in
    sdd.toml installs without them (the user opted out of the tested set).
    """
    spec = f"{PACKAGE}=={version}"
    pinned = version == DEFAULT_VERSION and CONSTRAINTS.is_file()
    lock = ["-c", str(CONSTRAINTS)] if pinned else []
    commands = []
    if shutil.which("uv"):
        commands.append(["uv", "tool", "install", spec, *lock])
    if shutil.which("pipx"):
        pip_args = [f"--pip-args=-c {shlex.quote(CONSTRAINTS.as_posix())}"] if pinned else []
        commands.append(["pipx", "install", spec, *pip_args])
    # -I: isolated mode, so nothing from the working directory or PYTHON* variables is used.
    commands.append(
        [sys.executable, "-I", "-m", "pip", "install", "--user", "--quiet", spec, *lock]
    )
    return commands


def install(version: str, run: Runner = _run) -> str | None:
    """Install the pinned graphify with the first tool that works; return its path.

    Installers run from the user's home directory, never from the repository.
    """
    for cmd in install_commands(version):
        if run(cmd, Path.home(), INSTALL_TIMEOUT) == 0 and (exe := find_graphify()):
            return exe
    return None


def _head(root: Path) -> str:
    try:
        proc = subprocess.run(
            [*gitsafe.GIT, "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def graph_status(root: Path) -> str:
    """``missing``, ``stale`` (built from another commit) or ``fresh``."""
    report = root / OUT_DIR / "GRAPH_REPORT.md"
    if not (root / OUT_DIR / "graph.json").is_file() or not report.is_file():
        return "missing"
    match = _BUILT_FROM.search(report.read_text(encoding="utf-8", errors="replace"))
    head = _head(root)
    if match and head and not head.startswith(match.group(1)):
        return "stale"
    return "fresh"


def ensure_ignores(root: Path) -> list[str]:
    """Write ``.graphifyignore`` if missing and keep ``graphify-out/`` out of git."""
    done = []
    ignore = root / IGNORE_FILE
    if not ignore.exists():
        ensure_safe(root, ignore)
        ignore.write_text(IGNORE_DEFAULTS, encoding="utf-8")
        done.append(f"wrote {IGNORE_FILE}")
    gitignore = root / ".gitignore"
    text = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    if not any(line.strip().rstrip("/") == OUT_DIR for line in text.splitlines()):
        prefix = "" if not text or text.endswith("\n") else "\n"
        ensure_safe(root, gitignore)
        gitignore.write_text(f"{text}{prefix}{OUT_DIR}/\n", encoding="utf-8")
        done.append(f"added {OUT_DIR}/ to .gitignore")
    return done


def sync(root: Path, version: str = DEFAULT_VERSION, run: Runner = _run) -> list[str]:
    """Install if needed, then build or refresh the graph. Callers check trust first:
    the version comes from sdd.toml, and graphify reads the repository."""
    done = ensure_ignores(root)
    exe = find_graphify()
    if exe is None:
        exe = install(version, run)
        if exe is None:
            return [*done, f"could not install {PACKAGE}=={version} (tried uv, pipx, pip)"]
        done.append(f"installed {PACKAGE}=={version}")
    status = graph_status(root)
    if status == "fresh":
        return [*done, "graph is up to date"]
    if run([exe, "update", "."], root, BUILD_TIMEOUT) != 0:
        return [*done, f"`graphify update .` failed; see {LOG_FILE.as_posix()}"]
    return [*done, f"graph {'built' if status == 'missing' else 'refreshed'}"]


def needs_sync(root: Path) -> bool:
    return find_graphify() is None or graph_status(root) != "fresh"


def _locked(root: Path) -> bool:
    lock = root / LOCK_FILE
    try:
        return time.time() - lock.stat().st_mtime < LOCK_SECONDS
    except OSError:
        return False


def start_background_sync(root: Path, entry: Path) -> bool:
    """Run ``sdd graph sync`` detached, so a session never waits for it. One at a time."""
    if _locked(root) or not needs_sync(root):
        return False
    try:
        ensure_safe(root, root / LOCK_FILE)
        ensure_safe(root, root / LOG_FILE)
    except ValueError:
        return False
    (root / LOCK_FILE).parent.mkdir(parents=True, exist_ok=True)
    (root / LOCK_FILE).write_text(str(os.getpid()), encoding="utf-8")
    flags = 0
    if sys.platform == "win32":  # a check mypy narrows on every OS
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    try:
        with (root / LOG_FILE).open("a", encoding="utf-8") as log:
            subprocess.Popen(  # noqa: S603 - fixed argv: this interpreter and the plugin's entry
                [sys.executable, str(entry), "graph", "sync"],
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=flags,
                start_new_session=os.name != "nt",
                close_fds=True,
            )
    except OSError:
        release_lock(root)  # a failed spawn must not block the next session for 30 minutes
        return False
    return True


def release_lock(root: Path) -> None:
    (root / LOCK_FILE).unlink(missing_ok=True)


def session_hint(root: Path) -> str:
    """What the SessionStart context tells Claude about the graph."""
    if (root / OUT_DIR / "graph.json").is_file():
        return (
            " A code knowledge graph is in graphify-out/. Before broad Read, Glob or Grep "
            'sweeps, use `graphify query "<question>"`, `graphify explain "<symbol>"` or '
            "graphify-out/GRAPH_REPORT.md, then open only the files and lines it points to."
        )
    return " The code graph (graphify-out/) is being built in the background."
