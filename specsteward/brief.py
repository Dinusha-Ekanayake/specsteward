"""A short brief per task group, so a subagent reads one small file instead of the spec."""

import ast
import re
import shutil
import subprocess
from pathlib import Path

from specsteward.config import Config
from specsteward.remote import env_without_tokens
from specsteward.safefs import ensure_safe
from specsteward.tasks import Task, parse_tasks

BRIEF_DIR = Path(".sdd") / "briefs"
GRAPHIFY_BUDGET = 400
# Size caps, so one broad plan cannot turn a brief into a copy of the codebase.
MAX_PLAN_LINES = 40
MAX_API_FILES = 12
MAX_SIGNATURES = 25
MAX_RULES = 15
RULE_WORDS = re.compile(r"\b(MUST|MUST NOT|NEVER)\b")
COMMON_ROOTS = ("src", "lib", "app", "tests", "scripts", "configs", "docs")
_NOT_SOURCE = {"node_modules", "venv", "dist", "build", "target", "graphify-out", "specs"}
_BACKTICKED = re.compile(r"`([^`]+)`")
_REQ_OR_AC = re.compile(r"^- (?:\[[ x]\] )?(?:REQ|AC)-")
_JS_EXPORT = re.compile(
    r"^export\s+(?:default\s+)?(?:async\s+)?(?:function|class|const|interface|type)\s+\w+[^{=]*"
)

# The rules and the report format live in agents/implementer.md, a system prompt that is
# cached across a run, so they are not repeated in every brief.
FOOTER = "## Gate\n`{sdd} check` must print `SDD CHECK: PASS`. Report as your instructions say.\n"


def _is_specific(part: str) -> bool:
    """Module paths, snake_case and CamelCase names; plain words match too many lines."""
    return "/" in part or "_" in part or re.search(r"[a-z][A-Z]", part) is not None


def _tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for item in _BACKTICKED.findall(text):
        tokens.add(item)
        tokens.update(part for part in re.split(r"[.:\s]", item) if _is_specific(part))
        # `pkg.func` usually points at a file named pkg.<ext> in the plan.
        module, dot, _ = item.partition(".")
        if dot and module.isidentifier() and "/" not in item:
            tokens.add(f"/{module}.")
    return tokens


def _matching_lines(path: Path, tokens: set[str]) -> list[str]:
    if not path.exists() or not tokens:
        return []
    return [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if any(token in line for token in tokens)
    ]


def signatures(path: Path) -> list[str]:
    """Public top-level names: Python via ast, JS/TS via ``export`` lines."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix != ".py":
        return [m.group(0).strip() for line in text.splitlines() if (m := _JS_EXPORT.match(line))]
    try:
        body = ast.parse(text).body
    except SyntaxError as error:
        return [f"(could not parse: {error.msg}, line {error.lineno})"]
    out = []
    functions = ast.FunctionDef | ast.AsyncFunctionDef
    for node in body:
        if not isinstance(node, functions | ast.ClassDef) or node.name.startswith("_"):
            continue
        if isinstance(node, functions):
            ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            head = f"{prefix} {node.name}({ast.unparse(node.args)}){ret}"
        else:
            head = f"class {node.name}"
        doc = (ast.get_docstring(node) or "").split("\n")[0]
        out.append(f"{head}  # {doc}" if doc else head)
    return out


def _graphify(root: Path, query: str) -> str:
    exe = shutil.which("graphify")
    if exe is None or not (root / "graphify-out").exists() or not query:
        return ""
    try:
        proc = subprocess.run(
            [exe, "query", query, "--budget", str(GRAPHIFY_BUDGET)],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=60,
            env=env_without_tokens(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return "\n".join(line for line in proc.stdout.splitlines() if line.startswith("NODE"))


def path_roots(cfg: Config) -> tuple[str, ...]:
    """Configured roots, or common source folders plus the project's own top-level folders
    (so flat layouts such as ``mypkg/`` work, and so does a ``src/`` the plan creates)."""
    if cfg.path_roots:
        return cfg.path_roots
    existing = {
        entry.name
        for entry in cfg.root.iterdir()
        if entry.is_dir()
        and not entry.name.startswith((".", "_"))
        and entry.name not in _NOT_SOURCE
    }
    return tuple(sorted(existing | set(COMMON_ROOTS)))


def _capped(lines: list[str], limit: int, what: str) -> list[str]:
    if len(lines) <= limit:
        return lines
    return [*lines[:limit], f"(+{len(lines) - limit} more {what} cut; see BRIEF GAPS)"]


def _api_section(cfg: Config, plan_lines: list[str]) -> list[str]:
    roots = "|".join(re.escape(r) for r in path_roots(cfg))
    if not roots:
        return []
    path_re = re.compile(rf"(?:{roots})/(?:[\w-]+/|[\w-][\w.-]*/)*[\w-][\w.-]*\.\w+")
    named = set(path_re.findall("\n".join(plan_lines)))
    # A new module should reuse what its package already has, so show its siblings too.
    siblings = {
        sibling.relative_to(cfg.root).as_posix()
        for rel in named
        if not (cfg.root / rel).exists() and (cfg.root / rel).parent.is_dir()
        for sibling in (cfg.root / rel).parent.glob(f"*{Path(rel).suffix}")
        if not sibling.name.startswith("__init__")
    }
    apis = []
    chosen = sorted(named | siblings | set(cfg.helper_files))
    for rel in chosen[:MAX_API_FILES]:
        path = cfg.root / rel
        if not path.exists():
            apis.append(f"### {rel}\n(new file)")
        elif path.suffix in (".py", ".ts", ".tsx", ".js", ".mjs"):
            names = _capped(signatures(path) or ["(no public names)"], MAX_SIGNATURES, "names")
            apis.append(f"### {rel}\n" + "\n".join(names))
    if len(chosen) > MAX_API_FILES:
        apis.append(f"(+{len(chosen) - MAX_API_FILES} more files cut; see BRIEF GAPS)")
    return apis


def build_brief(cfg: Config, spec_dir: Path, task_ids: list[str], sdd: str = "sdd") -> str:
    tasks = {t.id: t for t in parse_tasks(spec_dir / "tasks.md")}
    missing = [i for i in task_ids if i not in tasks]
    if missing:
        raise ValueError(f"unknown tasks: {', '.join(missing)} (see the ID column of tasks.md)")
    chosen: list[Task] = [tasks[i] for i in task_ids]
    tokens = set().union(*(_tokens(f"{t.text} {t.test}") for t in chosen))
    plan_lines = _capped(_matching_lines(spec_dir / "plan.md", tokens), MAX_PLAN_LINES, "lines")
    spec_lines = (spec_dir / "spec.md").read_text(encoding="utf-8").splitlines()
    requirements = [line for line in spec_lines if _REQ_OR_AC.match(line)]

    constitution = spec_dir.parent / "constitution.md"
    rules = [
        line.strip()
        for line in (
            constitution.read_text(encoding="utf-8") if constitution.is_file() else ""
        ).splitlines()
        if RULE_WORDS.search(line) and line.strip().startswith(("-", "*"))
    ]
    risk = chosen[0].risk
    rows = "\n".join(f"| {t.id} | {t.text} | {t.test} | {t.risk} |" for t in chosen)
    parts = [
        f"# Brief: {', '.join(task_ids)} ({spec_dir.name})",
        f"Risk: {risk}. Reviewer model: {cfg.models[risk]}.",
        "## Tasks\n| ID | Task | Test | Risk |\n|---|---|---|---|\n" + rows,
        "## Plan lines\n" + ("\n".join(plan_lines) or "(none matched)"),
        "## Requirements and acceptance criteria\n" + ("\n".join(requirements) or "(none)"),
        *(
            [
                "## Project rules (constitution; a violation is CRITICAL)\n"
                + "\n".join(_capped(rules, MAX_RULES, "rules"))
            ]
            if rules
            else []
        ),
        "## Existing APIs (signatures only)\n" + ("\n\n".join(_api_section(cfg, plan_lines))),
    ]
    if cfg.graphify:
        query = " ".join(sorted(t for t in tokens if "/" not in t and " " not in t)[:8])
        nodes = _graphify(cfg.root, query)
        if nodes:
            parts.append("## Related graph nodes\n" + nodes)
    parts.append(FOOTER.format(sdd=sdd))
    return "\n\n".join(parts) + "\n"


def write_brief(cfg: Config, spec_dir: Path, task_ids: list[str], sdd: str = "sdd") -> Path:
    text = build_brief(cfg, spec_dir, task_ids, sdd)  # rejects unknown ids before any write
    path = cfg.root / BRIEF_DIR / f"{task_ids[0]}.md"
    ensure_safe(cfg.root, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
