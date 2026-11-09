"""The quality gate: the project's check commands plus a banned-wording scan.

Prints a short summary instead of full tool output, and a failure signature so the
orchestrator can spot the same failure coming back.
"""

import hashlib
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from specsteward import state as run_state
from specsteward import trust
from specsteward.config import CONFIG_FILE, CheckStep, Config, Wording
from specsteward.remote import CHANNELS

MAX_DETAIL_LINES = 25
COVERAGE_LINE = re.compile(r"^(TOTAL\b|All files\b|Statements\s*:)")
_SKIP_DIRS = {".git", ".sdd", ".venv", "venv", "node_modules", "__pycache__", "dist", "build"}


@dataclass(frozen=True)
class StepResult:
    name: str
    ok: bool
    detail: tuple[str, ...]


PYTHON_PLACEHOLDER = "{python}"
_VENV_PYTHONS = (
    ".venv/Scripts/python.exe",
    ".venv/bin/python",
    "venv/Scripts/python.exe",
    "venv/bin/python",
)


def _committed(root: Path, rel: str) -> bool:
    """True when git tracks ``rel``: a real virtual environment is never committed."""
    try:
        proc = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", rel],
            cwd=root,
            capture_output=True,
            check=False,
        )
    except OSError:
        return False
    return proc.returncode == 0


def project_python(root: Path) -> str:
    """The project's virtual environment Python, else the interpreter running sdd.

    A venv counts only if it looks like one (``pyvenv.cfg``) and is not in git, so a cloned
    repository cannot ship the interpreter the gate and the format hook run.
    """
    for candidate in _VENV_PYTHONS:
        venv = root / candidate.split("/")[0]
        if (
            (root / candidate).is_file()
            and (venv / "pyvenv.cfg").is_file()
            and not _committed(root, candidate)
        ):
            return str(root / candidate)
    return sys.executable


def resolve_cmd(cmd: tuple[str, ...], root: Path) -> list[str]:
    """Find the executable the way a shell would (``npm`` is ``npm.cmd`` on Windows).

    Only a name with a path separator (``./run.sh``, ``.venv/bin/python``) is looked up in
    the repository. A bare name such as ``python`` always comes from ``PATH``, so a file
    committed as ``python`` at the repository root cannot stand in for it.
    """
    exe = project_python(root) if cmd[0] == PYTHON_PLACEHOLDER else cmd[0]
    has_sep = "/" in exe or "\\" in exe
    local = root / exe
    found = str(local) if has_sep and local.is_file() else shutil.which(exe)
    if found is None:
        raise FileNotFoundError(exe)
    return [found, *cmd[1:]]


def without_bot_tokens() -> dict[str, str]:
    """This environment minus the phone bot tokens. In a `claude-phone` session the token
    is in Claude Code's environment for the channel plugin; the project's checks and
    formatters (code an agent may have written) have no use for it."""
    secret = {channel.token_var for channel in CHANNELS}
    return {key: value for key, value in os.environ.items() if key not in secret}


def run_step(root: Path, step: CheckStep) -> StepResult:
    try:
        proc = subprocess.run(
            resolve_cmd(step.cmd, root),
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=step.timeout,
            env=without_bot_tokens(),
        )
    except FileNotFoundError as error:
        return StepResult(step.name, False, (f"command not found: {error}",))
    except subprocess.TimeoutExpired:
        return StepResult(step.name, False, (f"timed out after {step.timeout}s",))
    lines = (proc.stdout + proc.stderr).splitlines()
    keep = re.compile(step.keep) if step.keep else None
    detail = [line for line in lines if keep and keep.search(line)][:MAX_DETAIL_LINES]
    if proc.returncode != 0 and not detail:
        detail = lines[-MAX_DETAIL_LINES:]
    detail += [line for line in lines if COVERAGE_LINE.search(line)][:1]
    return StepResult(step.name, proc.returncode == 0, tuple(detail))


def _negated(line: str, start: int, negations: tuple[str, ...]) -> bool:
    """True when one of the three words before the match is a negation."""
    before = re.findall(r"[A-Za-z']+", line[:start])[-3:]
    return any(word.lower() in negations for word in before)


def scan_wording(root: Path, wording: Wording) -> StepResult:
    if not wording.banned:
        return StepResult("wording", True, ())
    patterns = [re.compile(p, re.IGNORECASE) for p in wording.banned]
    hits = []
    for top in wording.scan:
        for path in sorted((root / top).rglob("*")):
            if path.suffix not in wording.suffixes or _SKIP_DIRS & set(
                path.relative_to(root).parts
            ):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:  # a directory named like a source file, or a dangling symlink
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if wording.allow_marker in line:
                    continue
                if any(
                    not _negated(line, match.start(), wording.negations)
                    for pattern in patterns
                    for match in pattern.finditer(line)
                ):
                    rel = path.relative_to(root).as_posix()
                    hits.append(f"{rel}:{number}: {line.strip()[:120]}")
    return StepResult("wording", not hits, tuple(hits[:MAX_DETAIL_LINES]))


def signature(results: list[StepResult]) -> str:
    """Hash of the failing lines with line numbers and timings removed."""
    parts = []
    for result in results:
        if result.ok:
            continue
        for line in result.detail:
            if COVERAGE_LINE.search(line):
                continue
            line = re.sub(r":\d+(?::\d+)?", ":", line)
            line = re.sub(r"\b\d+(?:\.\d+)?m?s\b", "", line)
            parts.append(f"{result.name}|{line}")
    return hashlib.sha1("\n".join(parts).encode()).hexdigest()[:12] if parts else ""


def format_report(results: list[StepResult]) -> str:
    ok = all(r.ok for r in results)
    summary = ", ".join(f"{r.name} {'ok' if r.ok else 'FAIL'}" for r in results)
    out = [f"SDD CHECK: {'PASS' if ok else 'FAIL'} ({summary or 'no steps configured'})"]
    for result in results:
        coverage = [line for line in result.detail if COVERAGE_LINE.search(line)]
        out += [f"coverage: {' '.join(line.split())}" for line in coverage]
        if not result.ok:
            out.append(f"--- {result.name}")
            out += [line for line in result.detail if not COVERAGE_LINE.search(line)]
    if not ok:
        out.append(f"SIGNATURE: {signature(results)}")
    return "\n".join(out)


UNTRUSTED = (
    f"{CONFIG_FILE} is not trusted (new, or changed since `sdd trust`). Its commands were "
    f"not run. Review {CONFIG_FILE}, then run `sdd trust` (it asks for permission)."
)


def guard_step(cfg: Config) -> StepResult:
    """Fail when a gate config file changed since the run started (or since ``sdd trust``).

    Only during an active run: outside one, editing the project's config is ordinary work.
    """
    current = run_state.load(cfg.root)
    if current is None or not current["active"]:
        return StepResult("guard", True, ())
    changed = trust.changed_guard(cfg.root, cfg.guard)
    if not changed:
        return StepResult("guard", True, ())
    detail = [f"gate config changed during the run: {rel}" for rel in changed]
    detail.append("revert it, or ask the user to review the change and run `sdd trust`")
    return StepResult("guard", False, tuple(detail))


NO_STEPS = (
    f"{CONFIG_FILE} has no [[check]] steps, so the gate would prove nothing. Add your test "
    'command, for example: [[check]] name = "tests" cmd = ["make", "test"]'
)


def run_all(cfg: Config) -> list[StepResult]:
    if not trust.config_trusted(cfg.root):
        return [StepResult("trust", False, (UNTRUSTED,))]
    if not cfg.check:
        return [StepResult("steps", False, (NO_STEPS,))]
    results = [run_step(cfg.root, step) for step in cfg.check]
    return [*results, scan_wording(cfg.root, cfg.wording), guard_step(cfg)]
