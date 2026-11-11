# FAQ and troubleshooting

Start with `bash .sdd/bin/sdd doctor`: it checks the whole setup and prints a fix for every problem. The messages below are quoted exactly as specsteward prints them, so you can search this page for the one you see.

- [Using specsteward](#using-specsteward)
- [Setup problems](#setup-problems)
- [Approval problems](#approval-problems)
- [When a run halts](#when-a-run-halts)
- [Gate and task problems](#gate-and-task-problems)
- [Phone control problems](#phone-control-problems)
- [Code graph problems](#code-graph-problems)

## Using specsteward

**What does specsteward actually do?**
You approve a spec; subagents build it task by task, test first. A scripted gate (your own lint, types and tests) must pass and a reviewer must sign off before a task is ticked, and you stay in charge at every gate. See [How it works](how-it-works.md).

**How is it different from GitHub's Spec Kit?**
Spec Kit helps you write specs and drive an agent through them. specsteward borrows its clarify, analyze, constitution and converge ideas, and adds unattended execution: a scripted gate, risk-based reviewers, caps that stop runaway loops, gates an agent cannot edit around, a token budget, and approvals from your phone.

**Can I use it on an existing project?**
Yes. `/specsteward:init` reads the tools the project already has. The planner reads the code (through the code graph) before writing a spec.

**Do I have to use the planner? Can I write specs myself?**
Write them yourself if you like: copy `specs/_template/` to `specs/004-my-feature/`, fill it in, run `bash .sdd/bin/sdd analyze 004`, then approve.

**Do I need Telegram or Discord?**
No. Everything works in the terminal. Phone control is for approving and answering while you are away.

**Which models does it use, and can I change them?**
Planner and converger: Opus. Implementer: Sonnet. Clerk: Haiku. Reviewer: Sonnet for low risk, Opus for high and human risk (`[models]` in `sdd.toml`). Run your own session on Sonnet (`/model sonnet`).

**How do I keep the cost down?**
Run the session on Sonnet; set `[limits] max_tokens` to stop a run at a budget; optionally `[review] skip = ["low"]` to leave low-risk tasks to the final review. `bash .sdd/bin/sdd status` shows the tokens used.

**How do I see progress?**
`bash .sdd/bin/sdd status`, the status line (`bash .sdd/bin/sdd init --statusline`), or open `.sdd/dashboard.html` (it refreshes itself).

**Can I use specsteward in several projects at once?**
Yes. Each project keeps its own run in its own `.sdd/`, and trust and approvals are stored per repository, so projects never mix. Phone messages start with `[project name]`. Note that one chat bot can talk to only one Claude Code session at a time (see below).

**What if two Claude Code sessions are open on the same project?**
A run belongs to the session that started, resumed or claimed it. Only that session is told to continue, and only its subagents and tokens count. Other sessions are told the run belongs to another session. To continue it in a new session (for example after restarting Claude Code), run `bash .sdd/bin/sdd claim`.

**A task shows `[?]`. How do I accept it?**
It is a human task waiting for your judgement. Look at what was prepared, then run `bash .sdd/bin/sdd signoff T-003-06` (or send `signoff T-003-06` from your phone). It asks for your permission. Do not edit `tasks.md` by hand: that halts the run as a table changed outside `sdd done`.

**How do I stop a run?**
`bash .sdd/bin/sdd halt "reason"` pauses it; `bash .sdd/bin/sdd stop` ends it. From the phone, send `stop`.

**Does it commit or push on its own?**
No. When you say `ship <NNN>`, commit, push and opening the PR each ask for your permission.

**Is my code sent anywhere?**
specsteward has no telemetry. Your code goes to Claude as in any Claude Code session. With phone control, short summaries and the spec or plan files you ask for are sent to your chat app.

**Can I run two specs at once?**
Yes, one per git worktree: `bash .sdd/bin/sdd worktree 005`.

**Why so many permission prompts?**
Only the gates prompt: approving a spec, trusting `sdd.toml`, starting or resuming a run (they reset counters), commit, push and PR. Other shell commands (your test runner, `graphify`) prompt too unless you allow them in `.claude/settings.json`; `init` pre-allows only `bash .sdd/bin/sdd` and a few read-only `git` commands. The gate prompts are the guarantee that an agent cannot approve its own work.

**Claude Code shows `WSL ... execvpe(/bin/bash) failed` on Windows.**
A hook called a bare `bash`, and Windows found `C:\Windows\System32\bash.exe`, the WSL launcher, before Git's bash. It fails when no Linux distribution is installed. Releases before 0.6.0 started their hooks that way, so on an affected PATH the guards did not run at all. Update the plugin (`claude plugin marketplace update specsteward`, then `claude plugin update specsteward@specsteward`) and restart Claude Code: the hooks now go through `scripts/hooks/run.cmd`, which finds Git Bash itself (`CLAUDE_CODE_GIT_BASH_PATH`, next to `git.exe`, the usual install folders) and never uses the WSL stub. If Git for Windows is missing, the hook says so and does nothing.

**`sdd doctor` says specsteward hooks are in `.claude/settings.json` and in the plugin.**
Releases 0.3.2 and 0.3.3 wrote the hooks into each project; the plugin now ships them, so each would run twice. Run `/specsteward:init` once in the project (it removes only those seven entries) and restart Claude Code.

**Claude says it cannot edit `.claude/settings.json`.**
By design: in a specsteward project, file tools may not change the permission rules that make the gates. Edit the file yourself or use `/config`.

## Setup problems

**`specsteward: Python 3.11+ not found.`**
Install Python 3.11+ from python.org, or install [uv](https://docs.astral.sh/uv/) and specsteward uses a uv-managed Python, or set `SDD_PYTHON` to a Python 3.11+ executable.

**Hooks fail on Windows with `bash: command not found`.**
Install [Git for Windows](https://git-scm.com/download/win) (it includes bash) and restart Claude Code.

**`no sdd.toml here or above; set the project up with /specsteward:init`**
You are outside a set-up project. Open Claude Code in the project root, or run `/specsteward:init`.

**`sdd.toml is not trusted (new, or changed since sdd trust). Its commands were not run.`**
The gate only runs commands from an `sdd.toml` you trusted. After you (or a teammate's commit) change it, review it and run `bash .sdd/bin/sdd trust`. This also happens after moving or renaming the project folder.

**`pnpm not found (needed by 'lint', 'types', 'tests')`** (or any tool)
Install that tool, or change the `cmd` of those `[[check]]` steps to what your project uses, then `bash .sdd/bin/sdd trust`.

**`the gate has no [[check]] steps, so it proves nothing`**
`init` found no test commands. Add at least your test command to `sdd.toml`, for example:
```toml
[[check]]
name = "tests"
cmd = ["npm", "test"]
```
then `bash .sdd/bin/sdd trust`.

**`not a git repository`**
Run `git init` in the project root. Reviews read `git diff`, and runs use branches.

**`trust store is locked (...); try again`**
Another session was writing the trust store at the same moment. Try again; a lock left behind by a crashed process clears itself after 30 seconds.

**`SDD_HOME (...) must be outside the project (...)`**
`SDD_HOME` moves the trust store; it may not point inside the project, where agents can write. Unset it or point it elsewhere.

## Approval problems

**`resolve the CRITICAL findings first (or pass --anyway)`**
`bash .sdd/bin/sdd analyze <NNN>` lists them: usually an unanswered `[NEEDS CLARIFICATION]` question or a template placeholder. Answer or fix them, then approve again.

**`there is no spec 003 in specs/; draft one with /specsteward:run plan <topic>`**
Check the number with `ls specs`. Spec numbers are three digits (`003`, not `3`).

**`.../spec.md says approved but has no recorded approval; run sdd approve 003`**
The status line was set to Approved by hand (or by an agent). Only `sdd approve` records an approval.

**`... (spec, plan or task table) changed after it was approved; the user must approve it again`**
An approval covers the spec, the plan and the task definitions exactly as approved. Review what changed (`git diff specs/`), then `bash .sdd/bin/sdd approve <NNN> --start`.

**`switch to a feature/003-* or fix/003-* branch first`**
Runs happen on the spec's branch: `git checkout -b feature/003-rate-limit`. To turn the check off, set `branch_prefix = ""` in `[project]`.

**`a run for spec 003 is already active; continue it with sdd next ...`** / **`...; run sdd stop first`**
Only one run per checkout. Resume the active one, stop it, or use `bash .sdd/bin/sdd worktree <NNN>` for parallel specs.

## When a run halts

A halt means the run needs you. `bash .sdd/bin/sdd status` shows the reason. Deal with it, then `bash .sdd/bin/sdd resume` (it asks you first, because it resets the task's counters).

| Halt reason | What it means | What to do |
|---|---|---|
| `T-...: same check failure twice (...)` | The implementer's fix did not change the failure | Look at `bash .sdd/bin/sdd check`; give a hint or adjust the task, then resume |
| `T-...: fix attempt cap reached` | More than `max_fix_attempts` failed gates | The task is probably too big or underspecified; split or clarify it |
| `T-...: review round cap reached with open findings` | The reviewer still has CRITICAL or HIGH findings | Read the findings; decide, then resume |
| `agent run cap reached (60)` | More subagent runs than `max_agent_runs` | Check progress; raise the cap if the spec is just large |
| `token budget reached (... of ...)` | Fresh tokens exceeded `max_tokens` | Raise the budget or stop the run |
| `no task completed since the last automatic continue` | The session stopped twice without a ticked task or a finished subagent (waiting for a subagent that is still working does not count) | Read the last messages; give direction, then resume |
| `... changed after it was approved ...` | Spec, plan or tasks changed during the run (for example by converge) | Review and `bash .sdd/bin/sdd approve <NNN> --start` |
| `tasks.md changed outside sdd done (...)` | A task row was ticked, removed or added by hand | Restore it (`git checkout -- specs/<NNN>-*/tasks.md`) or approve the new tasks |
| `converge round cap reached (2); ship or plan a new spec` | Converge keeps finding work | Ship what you have, or plan the rest as a new spec |
| a question from the planner, implementer or reviewer | A decision only you can make | Answer it (`answer: ...` from the phone), then resume |

## Gate and task problems

**`no passing sdd check for the code as it is now; run the gate again`**
`done` only ticks a task when the last passing gate saw the current code. Something changed since (often the reviewer's extra test). Run `bash .sdd/bin/sdd check` again.

**`the run is halted (...)`**
A halted run takes no review verdict, agent run or tick until you resume it. `bash .sdd/bin/sdd status` shows why it stopped; deal with that, then `bash .sdd/bin/sdd resume`.

**`changes outside the approved plan: ...`**
The run changed a file the plan's `Modules / files` table does not name. Revert the file, or add it to `plan.md` and approve the spec again (`bash .sdd/bin/sdd scope` lists the files). In `[scope] mode = "warn"` this is a warning.

**`unplanned changes inside planned files: ...`**
A public function, class or method was added or changed without being named in the plan or the task row. Name it there in backticks and approve again, or revert it. Only refused with `[scope] symbols = "enforce"`.

**`uncovered change: ...`**, **`new side effect: ...`**, **`large change: ...`**
The drift guard looks inside the files the plan names. `uncovered change`: lines the run changed that no test runs (`changed but no test runs them`); write the test and run the gate. `new side effect`: a new import of, or call to, a process, the network, code evaluation, file deletion or file writing; name it in the plan and approve again, or remove it. `large change`: a function grew past `[scope] max_growth`, or a large new function is not named in the plan. By default `uncovered change` and `new side effect` block a task marked `high` or `human` and are warnings for a `low` one (the message says `This blocks because the task is high risk`); `large change` is a warning. `enforce`, `warn` and `off` in `sdd.toml` override that.

**`the coverage report is older than the changes; run the gate again`** / **`the coverage report is not the one the last passing gate wrote`**
`[coverage]` reads the report your test step writes. It must come from the gate's own run on the code as it is now; run `bash .sdd/bin/sdd check` again.

**`git's exclude settings (.git/info/exclude or core.excludesFile) changed during this run`**
Those settings hide new files from git, so they would also hide them from the gate and the scope check. Undo the change, or `sdd stop` and start the run again.

**`no failing test run was recorded for this group`**
`[tdd] red_first`: the gate never saw a test fail before it passed. Run the failing test through `bash .sdd/bin/sdd check` before implementing. If no test applies, tick with `sdd done <task> --waive-red "reason"`.

**`no reviewer subagent has finished since the last clean verdict and passing gate`**
A clean verdict is only taken after a reviewer agent has really run on the code the gate passed. Run the reviewer (a new one, if a reused one was not counted), then record the verdict.

**`switch to a feature/003-* or fix/003-* branch first`**
A run starts only on the spec's branch. `approve --start` checks this before approving, so nothing was approved. `git checkout -b feature/003-<name>`, then approve again.

**`no clean review for the code as it is now; review it (again) first`**
The code changed after the review, or the review asked for changes. Review again.

**`files changed while the gate ran; run sdd check again`**
Something wrote files during the check. If your tests write files into the project, write them to a temporary folder or add them to `.gitignore`.

**`gate config changed during the run: pyproject.toml`**
A file that configures the gate (lint, type, test or coverage settings) changed during a run. If an agent did it, revert it. If you meant it, review the change and run `bash .sdd/bin/sdd trust`.

**`T-...: the Status cell must be the last column of the row`**
The task table needs `| ID | Task | Test | Risk | Status |`, with Status last.

**Tests pass but the code is wrong.**
The gate proves consistency, not correctness. Strengthen the acceptance criteria, mark the task `high` risk (Opus review, and the reviewer is asked for an adversarial test; the gate does not check that it did), and add the rule to `specs/constitution.md`.

## Phone control problems

**My reply on the phone never reached Claude, although messages from Claude arrive.**
Two things cause this. (1) The session was not started with the channel on: start Claude Code with `claude-phone` (installed by `sdd phone setup`), which passes your messages into the session; a plain `claude` session can still send but never receives. (2) Several sessions run the plugin, for example a terminal and VS Code panels, and the bot hands each message to just one of them. `bash .sdd/bin/sdd phone setup` fixes this for good by switching the plugin off in ordinary sessions, so only `claude-phone` sessions run the bot; `sdd doctor` warns while it is still on everywhere.

**The bot does not answer.**
Run `bash .sdd/bin/sdd phone setup` in a terminal; it pairs Telegram on your keyboard and checks the token. For Discord, start `claude-phone`, DM the bot, and pair with `/discord:access pair <code>`. Run `bash .sdd/bin/sdd remote` to see what is missing.

**`Bun is missing; phone control needs it`**
Run `bash .sdd/bin/sdd install bun`.

**`Bun is installed but not on PATH`**
Restart the terminal and Claude Code, so the channel plugin can start `bun`.

**`Bun's installer needs unzip; install it (for example sudo apt install unzip)`**
On Linux, install `unzip` with your package manager, then `bash .sdd/bin/sdd install bun` again.

**`dmPolicy is not 'allowlist'`** / **`more than one user can approve`**
Lock it down with `bash .sdd/bin/sdd phone lock`, which allows only your paired account.

**`claude-phone: could not read the bot token from the credential store`**
The keychain entry is gone or locked: on macOS unlock the login Keychain, on Linux make sure your desktop keyring is unlocked. If you moved to a new Windows account or machine, the DPAPI blob cannot be decrypted there. In every case, `bash .sdd/bin/sdd phone setup` again stores the token anew.

**`the telegram bot token is in a plain-text file`** (from `sdd doctor`)
It was saved by `/telegram:configure` or an older version. Run `bash .sdd/bin/sdd phone setup` in a terminal and keep the bot: it moves the token into the credential store and deletes the plain-text copy.

**Is my bot token safe?**
It sits in your operating system's credential store, encrypted, and reaches the plugin only when `claude-phone` starts. See [Where the token is kept](phone.md#where-the-token-is-kept) for what that does and does not protect against, and how to replace a leaked token.

**Approvals do not reach the phone while I am away.**
The computer must stay on and awake during a run (on Windows: Settings > Power, sleep "Never" when plugged in).

## Code graph problems

**`code graph is missing` or `stale`**
It rebuilds in the background at session start. To do it now: `bash .sdd/bin/sdd graph sync`. Its log is `.sdd/graphify.log`.

**`could not install graphifyy==... (tried uv, pipx, pip)`**
Install [uv](https://docs.astral.sh/uv/) (recommended) and try `bash .sdd/bin/sdd graph sync` again. To turn the graph off, set `[graphify] auto = false`.

**I do not want anything installed automatically.**
Set `[graphify] auto = false` in `sdd.toml`, and do not set up phone control (Bun is installed only for it).

Still stuck? Open an issue with the output of `bash .sdd/bin/sdd doctor`: [new issue](https://github.com/Dinusha-Ekanayake/specsteward/issues/new/choose).
