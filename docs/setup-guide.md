# Setup guide

This guide takes you from nothing to a first spec built by specsteward. It takes about 15 minutes. Phone control (step 7) is optional.

If something goes wrong at any step, run `bash .sdd/bin/sdd doctor` and see the [FAQ and troubleshooting](faq.md).

## 1. Check what you need

| You need | Check | Get it |
|---|---|---|
| Claude Code | `claude --version` | [claude.com/claude-code](https://claude.com/claude-code) |
| `bash` | `bash --version` | macOS and Linux have it. On Windows it comes with [Git for Windows](https://git-scm.com/download/win), which Claude Code uses too |
| `git` | `git --version` | [git-scm.com](https://git-scm.com/) |
| Python 3.11 or newer | `python --version` (or `py -3 --version` on Windows) | [python.org](https://www.python.org/downloads/). If you have [uv](https://docs.astral.sh/uv/) instead, specsteward uses a uv-managed Python automatically |
| Your project's own tools | for example `npm test`, `pytest`, `go test` | as you normally would |
| `gh` (optional) | `gh --version` | [cli.github.com](https://cli.github.com/), only to open the pull request when you ship |

You do **not** need to install graphify or Bun yourself; specsteward installs pinned versions when they are needed.

## 2. Install the plugin

In Claude Code:

```text
/plugin marketplace add Dinusha-Ekanayake/specsteward
/plugin install specsteward@specsteward
```

Restart Claude Code once. You now have `/specsteward:init`, `/specsteward:run` and `/specsteward:remote`.

## 3. Set up your project

Open Claude Code in your project's root folder (where `.git` is) and run:

```text
/specsteward:init
```

It:

- detects the stack (Python, Node, Go, Rust, or a Makefile) and writes `sdd.toml` with the lint, type and test commands the project already uses;
- writes spec templates to `specs/_template/` and `specs/_template_fix/`, and `specs/constitution.md` for your project's rules;
- adds `.sdd/` and `graphify-out/` to `.gitignore`, and permission rules to `.claude/settings.json`;
- starts building the code graph in the background;
- runs `sdd doctor` and `sdd check`, and tells you what it found.

**Restart Claude Code** so the new permission rules load.

## 4. Review three files

1. **`sdd.toml`**: the `[[check]]` commands are your quality gate. Every task must pass them. Make sure they run your real lint, type checks and tests. If you change the file, run `bash .sdd/bin/sdd trust` afterwards (it asks you first).
2. **`specs/constitution.md`**: replace the examples with the rules your project must always follow, written as `- ... MUST ...` or `- NEVER ...`. They go into every brief, and a reviewer treats a violation as critical.
3. **`.claude/settings.json`**: has the gates as `ask` rules (approve, trust, start, resume, signoff, commit, push, PR). Leave them; you may remove `git commit` from `ask` if you want commits without a prompt.

Then run:

```text
bash .sdd/bin/sdd doctor
```

Everything should be `ok` or `warn`. Fix any `FAIL` with the fix it prints.

For unattended runs, also turn on Claude Code's sandbox (`"sandbox": {"enabled": true}` in your Claude Code settings): the gate runs tests the agent wrote, as you, and the sandbox is what contains them.

## 5. Plan your first spec

Run the orchestrating session on Sonnet (`/model sonnet`) to save tokens; agents that need Opus still use it.

```text
/specsteward:run plan "add rate limiting to the public API"
```

The planner (Opus) studies the project and writes `specs/001-<slug>/` with `spec.md`, `plan.md` and `tasks.md`. If something is unclear, it asks **up to five questions** with options; answer like `answer 001: 1a, 2 use Redis`.

For a bug instead of a feature:

```text
/specsteward:run fix "checkout crashes when the cart is empty"
```

That writes a small fix spec whose first task is a failing regression test.

## 6. Approve and run

Read the three files. Then check and approve:

```text
bash .sdd/bin/sdd analyze 001
bash .sdd/bin/sdd approve 001 --start
```

`analyze` lists problems (unanswered questions, requirements without tasks, vague wording). `approve` refuses while critical ones remain. The approval is a permission prompt only you can answer, and it covers the spec, plan and task table exactly as they are.

Switch to the spec's branch if `approve --start` asks you to (`feature/001-...` or `fix/001-...`), then:

```text
/specsteward:run 001
```

Each group of tasks gets a brief, an implementer writes tests first, the gate runs, a reviewer signs off, and the task is ticked. You see one line per group. The run stops and asks you when it needs a decision, and at the end:

1. a converger checks the code against the spec and may add tasks (you approve them, the run continues);
2. the clerk drafts the changelog entry and the PR description;
3. a final review of the branch runs;
4. you say `ship 001`, and commit, push and PR each ask you first.

Follow the run with `bash .sdd/bin/sdd status`, the status line (`bash .sdd/bin/sdd init --statusline`), or `.sdd/dashboard.html` in a browser.

## 7. Optional: approve from your phone

```text
/specsteward:remote
```

It installs everything in one step (Bun, the official Telegram or Discord plugin, and a `claude-phone` command) and switches the plugin off in ordinary sessions, so other terminals and VS Code panels cannot swallow your replies. For the rest, run `bash .sdd/bin/sdd phone setup` in a terminal: create the bot, paste the token, send it one message and press `y`; it locks the bot to you and can start `claude-phone`. Details: [phone.md](phone.md).

## 8. Optional: run specs in parallel

```text
bash .sdd/bin/sdd worktree 002
```

creates `../<repo>-002` on the spec's branch. Open Claude Code there and run `/specsteward:run 002`. Worktrees share trust and approvals; each has its own run.

## Upgrading

`/plugin marketplace update specsteward`, then `/plugin update specsteward@specsteward`, restart Claude Code, then run `/specsteward:init` once (it keeps your files, adds anything new and removes the project-level hooks that 0.3.2 and 0.3.3 wrote, which the plugin now ships itself) and `bash .sdd/bin/sdd doctor`. Read the [changelog](../CHANGELOG.md) for steps a release needs.

## Uninstalling

See [Uninstall](../README.md#uninstall) in the README.
