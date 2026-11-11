"""``sdd analyze``: cross-artifact checks of a spec before it is approved, with no model.

Inspired by GitHub Spec Kit's ``/analyze``, but scripted: unresolved clarifications and
template placeholders, requirements no task covers, tasks citing unknown requirements,
vague requirements with nothing measurable, and files the tasks name but the plan does not.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from specsteward.scope import planned_paths

CRITICAL, HIGH, MEDIUM, LOW = "CRITICAL", "HIGH", "MEDIUM", "LOW"
MAX_FINDINGS = 30
CLARIFY = re.compile(r"\[NEEDS CLARIFICATION[^\]]*\]", re.IGNORECASE)
PLACEHOLDER = re.compile(r"<(?:NNN|Title|slug|short description)>|\bTODO:|\bTBD\b|\?\?\?")
FIX_KIND = re.compile(r"\*\*Kind:\*\*\s*fix\b", re.IGNORECASE)
REQ_ID = re.compile(r"\bREQ-\d{3}-\d{2}\b")
REQ_LINE = re.compile(r"^- (?:\[[ x]\] )?(REQ-\d{3}-\d{2}|AC-\d+):?\s*(.*)$")
VAGUE = re.compile(
    r"\b(fast|quick|scalable|secure|robust|intuitive|user-friendly|efficient|simple|easy|"
    r"seamless|reliable|flexible|performant)\b",
    re.IGNORECASE,
)
PATH = re.compile(r"`([\w.-]+/[\w./-]+\.\w+)`")
COMMENT = re.compile(r"<!--.*?(?:--!?>|\Z)", re.DOTALL)


def without_comments(text: str) -> str:
    """Template guidance in <!-- --> is not content. Comments may span lines, end in `--!>`,
    or never close (then they run to the end, as in HTML). Each keeps its line breaks, so
    findings still point at the right line."""
    return COMMENT.sub(lambda match: "\n" * match.group().count("\n"), text)


ROW = re.compile(r"^\|\s*T-\d{3}-\d{2,}\s*\|")


@dataclass(frozen=True)
class Finding:
    level: str
    where: str
    message: str


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _markers(name: str, text: str) -> list[Finding]:
    found = []
    for number, line in enumerate(without_comments(text).splitlines(), start=1):
        if CLARIFY.search(line):
            found.append(Finding(CRITICAL, f"{name}:{number}", "unresolved clarification"))
        elif PLACEHOLDER.search(line):
            found.append(Finding(CRITICAL, f"{name}:{number}", "template placeholder left"))
    return found


def _requirements(spec: str) -> tuple[dict[str, str], list[str]]:
    reqs: dict[str, str] = {}
    acs = []
    for line in spec.splitlines():
        match = REQ_LINE.match(line.strip())
        if not match:
            continue
        key, text = match.groups()
        if key.startswith("REQ"):
            reqs[key] = text
        else:
            acs.append(text)
    return reqs, acs


def _coverage(reqs: dict[str, str], tasks: str) -> list[Finding]:
    rows = [line for line in tasks.splitlines() if ROW.match(line)]
    cited = {req for row in rows for req in REQ_ID.findall(row)}
    found = [
        Finding(HIGH, "tasks.md", f"{req} is cited but not defined in spec.md")
        for req in sorted(cited - reqs.keys())
    ]
    if reqs and not cited:
        message = "no task cites a requirement; name REQ ids in the Task or Test column"
        return [*found, Finding(MEDIUM, "tasks.md", message)]
    found += [Finding(HIGH, "spec.md", f"{req} has no task") for req in sorted(reqs.keys() - cited)]
    return found


def _vague(reqs: dict[str, str], acs: list[str]) -> list[Finding]:
    found = []
    for key, text in [*reqs.items(), *(("AC", t) for t in acs)]:
        word = VAGUE.search(text)
        if word and not re.search(r"\d", text):
            message = f"{key}: '{word.group(0)}' with nothing measurable"
            found.append(Finding(MEDIUM, "spec.md", message))
    return found


def _paths(tasks: str, plan: str) -> list[Finding]:
    rows = "\n".join(line for line in tasks.splitlines() if ROW.match(line))
    missing = sorted({p for p in PATH.findall(rows) if p not in plan})
    return [Finding(MEDIUM, "tasks.md", f"`{p}` is not in plan.md") for p in missing]


def _duplicates(reqs: dict[str, str]) -> list[Finding]:
    seen: dict[str, str] = {}
    found = []
    for key, text in reqs.items():
        norm = " ".join(text.lower().split())
        if norm and norm in seen:
            found.append(Finding(LOW, "spec.md", f"{key} repeats {seen[norm]}"))
        seen.setdefault(norm, key)
    return found


def _section(text: str, heading: str) -> str:
    """The body of a ``## heading`` section, without HTML comments."""
    match = re.search(rf"^## {heading}\s*$(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    return without_comments(match.group(1)).strip() if match else ""


def _fix_checks(spec: str, tasks: str) -> list[Finding]:
    """A fix spec says how to reproduce the bug and starts with a regression test."""
    if not FIX_KIND.search(spec):
        return []
    found = []
    steps = _section(spec, "Reproduction")
    if not steps or steps in ("1.", "-"):
        found.append(Finding(HIGH, "spec.md", "fix spec without reproduction steps"))
    rows = [line for line in tasks.splitlines() if ROW.match(line)]
    if not rows or "regression" not in rows[0].lower():
        found.append(Finding(HIGH, "tasks.md", "a fix starts with a failing regression test task"))
    return found


def analyze(spec_dir: Path) -> list[Finding]:
    spec, plan, tasks = (_read(spec_dir / n) for n in ("spec.md", "plan.md", "tasks.md"))
    found: list[Finding] = []
    for name, text in (("spec.md", spec), ("plan.md", plan), ("tasks.md", tasks)):
        if not text:
            found.append(Finding(CRITICAL, name, "missing"))
        found += _markers(name, text)
    reqs, acs = _requirements(spec)
    if spec and not reqs:
        found.append(Finding(HIGH, "spec.md", "no requirements (`- REQ-NNN-xx: ...`)"))
    if spec and not acs:
        found.append(Finding(MEDIUM, "spec.md", "no acceptance criteria (`- [ ] AC-n: ...`)"))
    found += _coverage(reqs, tasks) + _vague(reqs, acs) + _paths(tasks, plan)
    found += _duplicates(reqs) + _fix_checks(spec, tasks)
    if plan and not planned_paths(plan):
        found.append(
            Finding(
                HIGH,
                "plan.md",
                "names no files in backticks, so the scope guard has nothing to check; list "
                "each file the tasks create or change under `Modules / files`",
            )
        )
    order = {CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3}
    return sorted(found, key=lambda f: order[f.level])


def format_findings(spec_dir: Path, found: list[Finding]) -> str:
    counts = {level: sum(f.level == level for f in found) for level in (CRITICAL, HIGH, MEDIUM)}
    verdict = "BLOCKED" if counts[CRITICAL] else "OK"
    lines = [f"{f.level:<8} {f.where}: {f.message}" for f in found[:MAX_FINDINGS]]
    if len(found) > MAX_FINDINGS:
        lines.append(f"(+{len(found) - MAX_FINDINGS} more)")
    summary = ", ".join(f"{n} {level.lower()}" for level, n in counts.items())
    lines.append(f"SDD ANALYZE {spec_dir.name}: {verdict} ({summary})")
    return "\n".join(lines)
