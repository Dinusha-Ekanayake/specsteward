# Configuration (`sdd.toml`)

`sdd init` writes this file at the repository root. It detects the stack (Python, Node, Go, Rust, or a Makefile) and builds the gate from what the project already uses, so it normally works without editing. Run `sdd doctor` to check it. Every section is optional; the defaults are shown.

Any change to `sdd.toml` makes it **untrusted** until you run `sdd trust` (a permission prompt). Until then `sdd check` and the format hook run nothing. See [security.md](security.md).

## `[project]`

| Key | Default | Meaning |
|---|---|---|
| `specs_dir` | `"specs"` | Folder holding `<NNN>-<slug>/spec.md`, `plan.md`, `tasks.md` |
| `branch_prefix` | `"feature/{spec}-"` | `sdd start 003` requires a branch starting with `feature/003-`. Set `""` to turn the check off |
| `fix_branch_prefix` | `"fix/{spec}-"` | also accepted, for bug-fix specs (`/specsteward:run fix ...`) |
| `status_regex` | `(\*\*Status:\*\*\s*)([^\n]*)` | Finds the status line in `spec.md`. Group 1 is kept, group 2 is replaced on approval |
| `approved_word` | `"Approved"` | A spec counts as approved when its status starts with this **and** `sdd approve` recorded this exact content |
| `guard` | common gate config files | Files that configure the gate (`pyproject.toml`, `conftest.py`, `package.json`, `tsconfig.json`, eslint/jest/vitest configs, `Cargo.toml`, `go.mod`, `Makefile`, ...). Changing one during a run fails `sdd check` until you review it and run `sdd trust` |

## `[[check]]` (the quality gate)

One table per step, run in order. Commands are argument lists, not shell strings.

```toml
[[check]]
name = "tests"
cmd = ["{python}", "-m", "pytest", "-q", "--tb=line"]
keep = '^(FAILED|ERROR)'   # regex: which output lines to show when it fails
timeout = 900              # seconds; a hanging step fails instead of stalling the run
```

- `{python}` is the project's virtual environment (`.venv/` or `venv/`, on any OS) if it has one, else the Python running sdd. The same file works for everyone on the team.
- A command containing a path separator (`./scripts/lint.sh`) is looked up in the repository. A bare name (`npm`, `cargo`) always comes from `PATH`, so a file committed as `python` cannot replace the real one.
- If a step fails and `keep` matches nothing, the last 25 lines are shown.
- Lines like `TOTAL ... 96%` (coverage.py) or `All files` (Jest) are shown as a coverage line.
- The gate prints `SDD CHECK: PASS` or `FAIL` plus a `SIGNATURE`. The same signature twice halts the run.
- During a run the gate also has a `guard` step (see `[project] guard`).

## `[format]`

Formatters run after every file Claude edits inside the project, by suffix. `{file}` is the edited path.

```toml
[format]
".py" = [["{python}", "-m", "ruff", "format", "{file}"]]
```

## `[wording]`

```toml
[wording]
banned = ["failure predicted", '\bguaranteed\b']   # case-insensitive regexes
negations = ["not", "never", "no"]                 # a match right after one of these is allowed
scan = ["src", "docs"]
suffixes = [".py", ".md"]
allow_marker = "sdd: allow-wording"                # put this on a line to allow it
```

An empty `banned` list turns the scan off.

## `[models]`

Which model reviews tasks of each risk. The planner is Opus, the implementer Sonnet and the clerk Haiku (set in `agents/*.md`).

```toml
[models]
low = "sonnet"
high = "opus"
human = "opus"
```

## `[review]`

```toml
[review]
skip = []   # ["low"]: low-risk groups get no per-task review; the final branch review covers them
```

Only `"low"` is allowed. High and human risk are always reviewed.

## `[limits]`

| Key | Default | Halts when |
|---|---|---|
| `max_review_rounds` | 2 | a review still has open findings on the Nth round |
| `max_fix_attempts` | 3 | more than N gate failures for a task |
| `max_agent_runs` | 60 | more than N subagent runs in one spec (declared with `sdd agent` or observed by the SubagentStop hook, whichever is higher) |
| `restart_after_tasks` | 4 | (not a halt) start a fresh implementer after N completed tasks |
| `max_converge_rounds` | 2 | converge has added tasks N times and would run again (the run then goes on to ship) |
| `max_tokens` | 0 (off) | the run has used more than N fresh tokens (input, cache writes and output, from the session's and subagents' transcripts). Cached reads are shown in `sdd status` but not counted |

## `[groups]`

`max_size` (default 3) low-risk tasks, and `max_high` (default 2) high-risk tasks, go to the implementer in one run. `human` tasks always run alone.

## `[graphify]`

```toml
[graphify]
auto = true          # install graphify if missing and keep graphify-out/ fresh, in the background
version = "0.9.100"  # the pinned graphifyy release to install
```

With `auto = true`, every session start (and `sdd init`) runs a background sync: install the pinned [graphify](https://github.com/Graphify-Labs/graphify) with `uv tool`, `pipx` or `pip --user` if it is missing, write `.graphifyignore`, git-ignore `graphify-out/`, and build or refresh the graph when it is missing or built from another commit. The session context then tells Claude to query the graph before broad file searches. Building the code graph is local and costs no model tokens. `sdd graph status` and `sdd graph sync` do the same by hand.

## `[brief]`

| Key | Meaning |
|---|---|
| `helper_files` | Files always shown to the implementer (signatures only), for example `["tests/conftest.py"]` |
| `path_roots` | Top-level folders recognised when a plan names a path. Default: common source folders plus the project's own top-level folders |
| `graphify` | Add related graph nodes to briefs when `graphify-out/` exists (default `true`) |

Briefs are capped (40 plan lines, 12 files, 25 names per file); anything cut is marked so the implementer can list it under BRIEF GAPS.

## Permission rules written by `init`

`.claude/settings.json` gets, without removing anything already there:

- `allow`: `bash .sdd/bin/sdd` (everything not listed under `ask`), `git status`, `git diff`, `git log`, `git rev-parse`
- `ask`: `sdd approve`, `sdd trust`, `sdd start`, `sdd resume`, `sdd signoff`, `git commit`, `git push`, `gh pr create`, `gh pr merge`
- `deny`: `Edit(.sdd/**)`, `Edit(~/.claude/specsteward/**)`, `Edit(.claude/settings.json)`, `Edit(.claude/settings.local.json)`

`ask` wins over `allow`, so the gates always prompt. You may drop the `git` rules if you want commits without a prompt; `sdd doctor` will only warn. The `sdd` rules are the gates and `sdd doctor` fails without them.

`init` also removes the seven project-level specsteward hooks that 0.3.2 and 0.3.3 wrote (the plugin ships them now); any other hook in the file is left alone.

`sdd init --statusline` also adds the run to your status line in `.claude/settings.local.json` (an existing status line is kept; add `bash .sdd/bin/sdd statusline` to it yourself).
