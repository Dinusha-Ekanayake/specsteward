# Changelog

## 0.7.0 (2026-11-11)

### Changed
- **The drift guard follows the risk of the task.** `[scope] surface` and `[coverage] mode` now default to `auto`: a new side effect the plan does not name (a process, the network, code evaluation, file deletion or writing), or a changed line no test runs, blocks `sdd done` for a task marked `high` or `human` and stays a warning for a `low` one. Before, both were warnings for every task unless the project set `enforce`. The message says why it blocks and which setting turns it back into a warning. `enforce`, `warn` and `off` work as before, so a project that set one keeps its behaviour. Coverage is still only checked when `[coverage] report` names a report, `growth` stays a warning, and a run in a repository with no commit is not refused by `auto`.

## 0.6.0 (2026-11-10)

### Fixed
- **Windows: every hook failed when `System32` came before Git on `PATH`.** Hooks started `bash` by name, which Windows resolves to the WSL launcher (`C:\Windows\System32\bash.exe`) and which fails with `WSL ... execvpe(/bin/bash) failed` when no distribution is installed. On such machines the guards, the formatter and the run loop never ran, and Claude Code showed the error after every turn. Hooks now go through `scripts/hooks/run.cmd`, a file that works as a Windows batch file and as a POSIX script; it finds Git Bash itself and never uses the WSL stub, and the hook scripts run with the shell that is already running them (`"$BASH"`).
- Scope: a file that was already modified when the run started is compared by content (with a saved copy), so further changes to it are no longer invisible.
- Scope: a symbol is only named by the plan's `Modules / files` section, a `path::name` mention or a task row, not by any backticked word.
- Scope: `sdd check` prints `SCOPE:` lines early, and `sdd scope` shows which files only the allow-list permitted. `sdd doctor` says the scope check needs git.
- A clean review verdict needs a reviewer that finished after the last passing gate, not just any reviewer run.
- The setup wizard no longer calls a saved token dead when the network is down, says plainly when the token file is not owner-only (Windows), keeps a backup of an unreadable `access.json` instead of overwriting it, and no longer hands the launcher to WSL's `bash`.
- `package.json` of an unexpected shape no longer crashes `sdd init`, and Bun projects run their `test` script instead of Bun's own runner.
- A halted run now refuses review verdicts, agent runs and ticks until you resume it; before, `sdd done` still ticked a task after a cap had halted the run.
- `sdd approve <NNN> --start` on the wrong branch stops before approving anything, instead of approving and then failing to start.
- Mistakes in `sdd.toml` (an unknown key in `[limits]`, `[scope]`, `[coverage]`, `[tdd]` or `[review]`, which used to leave a guard silently on its default, text where a number belongs, a section that is not a table, a `branch_prefix` with an unknown placeholder) give one clear line instead of a Python traceback.
- A project in a subfolder of its git repository: scope and drift checks missed every change to a tracked file and reported it as outside the plan.
- A moved file is compared with the file it was, and a file with a byte-order mark is read correctly, instead of being treated as new or skipped.
- `[scope] mode = "warn"` now lists out-of-plan files at `sdd done`; `sdd analyze` reports a plan that names no files (the scope guard had nothing to check and said nothing).
- Every subcommand has a description and help for its arguments (`sdd <command> --help`).

### Added
- Drift guard for what a run does inside a function the plan already names (`specsteward/drift.py`). It narrows the gap the scope guard could not see; it does not prove the code correct.
  - `[coverage] report`: lines a run adds or changes must be run by a test, read from the coverage report your test step writes (Cobertura XML or lcov). A missing, unreadable or out-of-date report counts as a finding.
  - `[scope] surface`: a new import of, or call to, a process, the network, `eval`/`exec`, a file deletion or a write-mode `open` is listed unless the plan or tasks name it.
  - `[scope] growth` and `max_growth`: a function that gained more than the budget in syntax-tree nodes is listed.
  - All three default to `warn` (shown by `sdd check`, `sdd scope` and `sdd done`); `enforce` makes `sdd done` refuse.
- Red-before-green evidence: `[tdd] red_first` (`warn` by default, `enforce`, `off`). `sdd done` warns, or refuses, when a task group never had a failing test run before its passing one; `sdd done T --waive-red "reason"` records why no test applies.

### Changed
- `[scope] allow` patterns no longer treat `[` as a wildcard; write `?` or `*` instead of `[ab]`.
- Config regexes with a repeated group that itself repeats (`(\d+\.)+\d+`) are refused at load time as a risk of runaway matching; rewrite them without the nesting.

### Security
- Found by an independent audit and fixed:
  - Git cannot be used to hide a change or to run a program inside specsteward: every git call switches `core.fsmonitor` off and drops the phone bot token; files marked `--assume-unchanged` or `--skip-worktree` are compared by content; a change to `.git/info/exclude` or `core.excludesFile` during a run blocks `sdd done`; file tools may not write `.git/config`, `.git/hooks` or `.git/info`, and the shell guard asks before `git update-index --assume-unchanged` and before `git config` writes of settings that run programs.
  - The coverage report must be the file the last passing gate left behind, and a report that lists a file but none of its changed lines no longer passes. Side effects are followed through import aliases and counted; files named like tests outside a test folder are read; large new unnamed functions and nested classes count toward the growth budget.
  - The shell guard now looks through quoting, escapes, letter case, wildcards, `cd` into protected folders, `env`/`sudo`/`nohup` prefixes, `bash -lc`, here-documents and shells fed from a pipe, and asks for inline interpreter code that names run state or the environment. It also guards the Windows PowerShell tool (the hooks and the `ask` rules covered only Bash there).
  - Guard hooks keep guarding when `sdd.toml` is unparsable instead of going quiet.
  - `sdd.toml` is bound to the run, so swapping back an older trusted version halts the run until you trust it again.
  - `{python}` no longer resolves to a committed-symlink or script "venv", and bare commands are never taken from the working directory on Windows.
  - Scope sees renames and deletions, glob patterns no longer cross folders, and folders named like tools (`venv`, `node_modules`) are only skipped at the top level.
  - The bot token is removed from the environment of all child processes; `access.json` is written owner-only.
  - `sdd init --specs-dir` is validated; a generated config that runs the repository's own scripts is not trusted automatically; `ensure_safe` resolves `..`.
  - Config regexes with nested quantifiers are refused; hostile source files can no longer crash the symbol scan.
  - `phone`, `install`, `worktree` and `init` ask first; clean review verdicts need a witnessed reviewer once the hooks are known to be live.
  - CI: tool versions pinned in `requirements-dev.txt`, a `bandit` step, `CODEOWNERS`, Dependabot for pip.
- Phone pairing no longer trusts a display name: the wizard prints a random one-time code and pairs only the account that sends exactly that code. Text from strangers is printed without control or bidirectional characters.

## 0.5.0 (2026-11-10)

### Added
- Symbol check inside planned files: public functions, classes and methods that a run adds or changes in an existing file must be named in backticks in the plan or tasks. Python is compared by syntax tree, JavaScript and TypeScript see new exports. `[scope] symbols` is `warn` (default; printed on `sdd done`), `enforce` or `off`; `sdd scope` lists them too.

## 0.4.0 (2026-11-10)

### Added
- Scope guard: `sdd done` refuses while files outside the approved plan's `Modules / files` (plus tests, `CHANGELOG*`, the spec folder and `[scope] allow`) have changed since the run started. `sdd scope` lists them; `[scope] mode` is `enforce` (default), `warn` or `off`. Widening the scope means editing the plan, which breaks the approval hash and needs the user's approval again.
- Runs record the commit and dirty files at start, so earlier uncommitted work is not blamed on the run.

## 0.3.7 (2026-11-10)

### Fixed
- `.sdd/state.json` updates are serialised with a lock file (`.sdd/state.lock`, taken over after 30 seconds if a killed process left it), so two sessions or a hook running beside a command no longer overwrite each other's counters or verdicts.

## 0.3.6 (2026-11-10)

### Changed
- A clean review verdict (`sdd review <task> clean`) now needs a `specsteward:reviewer` subagent that the SubagentStop hook saw finish since the previous clean verdict, so an agent can no longer record a review nobody ran. Without hook evidence (older Claude Code) it stays honour-based.

## 0.3.5 (2026-11-10)

### Fixed
- File tools and shell commands now ask before changing a phone channel's `access.json` (the list of accounts that may answer permission prompts), so an agent cannot add an account and approve its own gates. It asks rather than blocks, because the official channel plugin's own skills edit that file.
- `.sdd/state.json` is renamed into place with the same retry the trust store uses, so a virus scanner or the dashboard holding the file on Windows no longer makes a write fail.

## 0.3.4 (2026-11-10)

### Changed
- The plugin ships its hooks again (`hooks/hooks.json`), so installing the plugin is enough: the guards, formatter, agent counter and session start work in every initialised project with no per-project hook setup, and they update with the plugin. Version 0.3.2 had moved them into each project's `.claude/settings.json`.
- `/specsteward:init` no longer writes hooks. It removes the exact seven specsteward hook entries that 0.3.2 wrote, and leaves every other hook and setting alone. Projects set up with 0.3.2 or 0.3.3: update the plugin, run `/specsteward:init` once, and restart Claude Code.

### Fixed
- `sdd init` no longer re-trusts a freshly written `sdd.toml` in a project the trust store already knows, so deleting a tuned config and running the allow-listed `init` cannot swap in the weaker default unnoticed.
- The shell guard now looks inside `bash -c "..."`, `sh -c`, and `eval` strings for gated `sdd` commands.
- The wording scan no longer skips every file when the project's own path contains a folder named `build` or `dist`, and no longer crashes on a directory or dangling link named like a source file.
- The change fingerprint now includes source packages named `out`, `coverage`, `dist` and so on below the top level; only top-level build output is skipped.
- A corrupt trust store is copied to `trust.json.corrupt` before it is reset, instead of being silently overwritten.
- `.sdd/state.tmp` gets the same symlink check as `state.json`; a malformed `state.json` means "no run" instead of a traceback.
- Task tables accept a lower-case `| id |` header and three-digit task numbers; a task row before the header, an invalid `status_regex`, and an unknown task id in `sdd brief` give clear errors (the last creates no directories).
- The pipx constraints path is quoted, so install paths with spaces work.
- Docs: the quick start says to switch to the spec's branch before `approve --start`; upgrade steps, permission rules, hook descriptions and uninstall steps now match the code.

### Added
- `sdd doctor` warns when those older project hooks are still present next to the plugin's, because each hook would run twice (and each agent would be counted twice).

## 0.3.3 (2026-11-09)

### Changed
- `plugin.json` now holds only fields Claude Code recognises; the four listing-only links (documentation, support, privacy policy, terms) were removed. The same pages are still linked from the README and `homepage`.
- README: the install section now covers the one-command install, updating, pinning a version and uninstalling.

## 0.3.2 (2026-11-08)

### Changed
- *(Superseded in 0.3.4: the plugin ships its hooks again.)* The plugin no longer declares hooks: the directory accepts command hooks only from Verified organizations. `/specsteward:init` now writes the same hooks into the project's `.claude/settings.json` (calling `.sdd/bin/sdd`). Projects set up with an earlier version must re-run `/specsteward:init` once, and restart Claude Code, to get the guards back.

## 0.3.1 (2026-11-05)

Ready for the Claude plugin directory.

### Changed
- The plugin's launcher moved from `bin/sdd` to `scripts/sdd.sh`. Claude Code puts a plugin's `bin/` folder on PATH, and the directory refuses commands it cannot follow. Projects need nothing: `.sdd/bin/sdd` is rewritten at the next session start.
- Hooks use exec form and run one-line wrappers in `scripts/hooks/` that hold only literal paths, as the directory requires. The launcher no longer reads `PWD` and no longer names the bot token variables.
- The plugin has an icon, and `plugin.json` links the setup guide, a privacy policy, a support page and the license for the directory listing.

### Added
- `PRIVACY.md`: no telemetry; everything is stored on your machine; every network connection specsteward makes is listed.
- `SUPPORT.md`: where to get help, report bugs and report security problems.

## 0.3.0 (2026-11-05)

### Added
- `sdd phone setup` (also run by `/specsteward:remote`) sets up phone control in one step: Bun, the official Telegram or Discord plugin, and a `claude-phone` command. It switches the plugin off in ordinary sessions and loads it only in `claude-phone` sessions (`--plugin-dir` plus `--channels`), so other terminals and VS Code panels no longer run competing copies of the bot that swallow replies. `sdd phone lock` restricts the bot to the paired account. `sdd doctor` warns while the plugin is still on in every session.
- In a terminal, `sdd phone setup` is a guided wizard: it asks for the bot token at a hidden prompt and checks it, then for Telegram waits for your first message, asks "Is this you?", locks the bot to that account and sends a test message. It then offers to start `claude-phone`. Telegram setup is now four human steps. `--no-input` keeps the old automatic-only behaviour.
- The bot token is kept in the operating system's credential store (Windows DPAPI, macOS Keychain, Linux Secret Service) instead of a plain-text file, and `claude-phone` passes it to the plugin as an environment variable at launch. A token already in `~/.claude/channels/<ch>/.env` is moved over when you run `sdd phone setup`. Machines with no credential store keep the owner-only file and are told so. `sdd doctor` warns about a plain-text token where a store exists, and `sdd check` and the format hook strip bot tokens from the environment of project commands.

- A shell-command guard (PreToolUse hook on Bash) asks you before gate commands however they are spelled, before shell writes to run state, the trust store or Claude settings, and before commands that read the bot token. It never denies.
- The ship gate shows `git diff --stat` against the base branch, so you see what the PR will change.

### Changed
- `sdd init` recognises Python projects without `pyproject.toml` (`requirements.txt`, `setup.cfg`, `Pipfile`, `tox.ini`, or plain `.py` files and `tests/test_*.py`), so the gate runs the tests from the start instead of needing a hand-edited `sdd.toml`.
- CI actions are pinned to commit SHAs and do not keep the checkout token.

### Fixed
- Approvals, trusted configs and the gate-config baseline no longer break when git converts line endings (CRLF and LF, on Windows or in teams mixing systems): fingerprints use normalized line endings, and records made by 0.2.0 still count.

## 0.2.0 - 2026-11-04

### Upgrading from 0.1.0
- The project is renamed from **sdd-pilot** to **specsteward** (another spec-driven project
  already used the old name). Remove the old plugin and install the new one:
  `/plugin uninstall sdd-pilot@sdd-pilot`, `/plugin marketplace add Dinusha-Ekanayake/specsteward`,
  `/plugin install specsteward@specsteward`. Commands are now `/specsteward:run`,
  `/specsteward:init` and `/specsteward:remote`; the `sdd` CLI, `sdd.toml` and `.sdd/` are
  unchanged, and `~/.claude/sdd-pilot/` moves to `~/.claude/specsteward/` by itself.
  The marketplace declares the rename (`renames`), so an existing `sdd-pilot` install
  migrates to `specsteward` on its next sync.
- Run `/specsteward:init` (or `bash .sdd/bin/sdd init`) once: it adds the new permission rules and `specs/constitution.md`, and keeps your `sdd.toml`.
- Review your `sdd.toml`, then run `bash .sdd/bin/sdd trust`. Until then `sdd check` and the format hook run nothing.
- Approve specs again (`sdd approve <NNN>`): approvals now cover the spec, plan and task table.
- A run started with 0.1.0 is not picked up; start it again with `sdd approve <NNN> --start` or `sdd start <NNN>`.
- `sdd done` now needs a passing `sdd check` and a clean `sdd review` of the current code first.
- Claude can no longer edit `.claude/settings*.json` with file tools in an sdd project; change those by hand or with `/config`.
- Run `bash .sdd/bin/sdd doctor` to confirm the setup.

### Security
- `sdd.toml` commands (the gate and the format hook) run only after `sdd trust`, which is a permission prompt; any change makes the file untrusted again. The trust store lives outside the repository (`~/.claude/specsteward/`).
- Spec approval is bound to a hash of `spec.md`, `plan.md` and the task definitions. Task statuses are recorded by `sdd start` and `sdd done`, so hand-ticked, deleted or re-risked rows halt the run.
- `sdd start`, `sdd resume` and `sdd trust` are permission prompts; `start` refuses while a run is active; `sdd init --force` never trusts the config it replaces.
- A PreToolUse hook and deny rules block file edits to `.sdd/`, the trust store and `.claude/settings*.json`, matching paths by file identity.
- `sdd done` requires a passing gate and a clean review recorded for the current tree, fingerprinted before and after the gate.
- Changing gate config files during a run fails the check; the reviewer flags new skips, ignores and lowered thresholds.
- graphify is installed only for a trusted `sdd.toml`, from the home directory in isolated mode.
- Repository-shipped virtual environments, symlinks and run state are ignored or refused.
- Every agent prompt treats repository content as data, not instructions.
- `sdd doctor` warns when Claude Code sandboxing is off, since the gate runs agent-written tests as you.

### Added
- `sdd init` detects the stack (Python, Node, Go, Rust, Makefile) and the project's own lint, type and test tools, so `sdd.toml` needs no editing; `{python}` resolves per machine.
- `sdd doctor`: checks every prerequisite and the setup, and prints a fix for each problem.
- Automatic graphify code graph, installed (pinned), built and refreshed in the background on every machine; sessions are told to query it before reading files. `sdd graph status|sync`.
- Automatic Bun install (pinned, per user, no admin rights) once phone control is set up; `sdd install bun`. The launcher falls back to a uv-managed Python when no Python 3.11+ is installed.
- `sdd statusline` (`sdd init --statusline`) and a read-only `.sdd/dashboard.html`.
- `sdd analyze`: scripted checks of spec, plan and tasks (clarifications, placeholders, requirement coverage, vague wording, plan paths); `sdd approve` refuses CRITICAL findings unless `--anyway`.
- Project constitution (`specs/constitution.md`): its MUST rules go into every brief; violations are CRITICAL in review.
- Clarify step: the planner asks up to five `[NEEDS CLARIFICATION]` questions before approval; answer with `answer <NNN>: ...`, also from the phone.
- `sdd approve <NNN> --start`: approve and start in one permission prompt.
- Docs: a step-by-step setup guide (`docs/setup-guide.md`) and an FAQ with troubleshooting for every error and halt message (`docs/faq.md`).
- `sdd --version`, help text for every command, plugin manifest homepage and repository, a supported-versions table in SECURITY.md, a privacy note, CodeQL code scanning, and least-privilege CI permissions.
- `[review] skip = ["low"]` to leave low-risk tasks to the final branch review.
- The launcher handles a Python path with spaces (for example `SDD_PYTHON` under `C:\Users\First Last\`); the clerk writes its PR draft next to the spec (`specs/<NNN>-*/pr.md`) instead of into `.sdd/`, which file tools may not write.
- `sdd signoff <task>` (an ask rule; `signoff <task>` from the phone) accepts a human task waiting at `[?]`; before, the only way was editing tasks.md, which halts the run.
- The implementer's own `sdd check` runs no longer count toward the failure caps (a failing test is how test-first starts); only the orchestrator's gate, `sdd check --task <id>`, does. Found in a dogfood run, where ordinary test-first work halted the run.
- Sessions: a run belongs to the session that starts, resumes or claims it (`sdd claim`); only that session is told to continue and only its subagents and tokens count, and other sessions are told whose run it is. Waiting for a subagent that is still working no longer counts as a stop without progress, a finished subagent counts as progress, and subagents of other tools are not counted. `sdd doctor` warns when several sessions run the same chat bot.
- Phone control over Discord as well as Telegram, through Anthropic's official channel plugins: `sdd remote` reports the ready channel (and others set up), `sdd doctor` checks each, Bun is installed for either; `docs/telegram.md` is now `docs/phone.md`.
- Parallel runs: `sdd worktree <NNN>` creates a sibling git worktree on the spec's branch (copying `sdd.toml` and the spec if they are not committed); trust and approvals are kept per repository, so worktrees share them, while each worktree has its own run. Several trusted `sdd.toml` versions can coexist.
- Bug-fix flow: `/specsteward:run fix <bug>` drafts a fix spec from `specs/_template_fix/` (reproduction, expected, actual, root cause; the first task is a failing regression test); `sdd analyze` checks both; `fix/<NNN>-*` branches are accepted.
- Converge (inspired by Spec Kit): after the last task, `agents/converger.md` compares the code with the spec, plan and constitution and appends missing work as new tasks; `sdd converge` counts rounds (`[limits] max_converge_rounds`) and gives the next task id; approving the appended tasks (`approve <NNN> --start`) resumes the run.
- Verified installs: Bun's install script comes from the pinned release tag and must match its pinned sha256; graphify installs with every dependency at an exact version (checked in CI on Linux, macOS and Windows).
- Token meter: the run's tokens (session and subagents) in `sdd status`, the status line and the dashboard, and an optional `[limits] max_tokens` budget that halts the run.
- Go and Rust presets.
- `sdd --help` groups the commands under five headings (setup, specs, running a spec, views and status, used by the agents during a run); hooks stay hidden and each command's help is unchanged.

### Changed
- Static rules and the report format moved from every brief into the implementer's agent prompt (about 140 words less per brief).
- Subagent runs are counted by a SubagentStop hook as well as `sdd agent`; the cap uses the higher count.
- `sdd check` records failures against the group in flight without `--task`.

### Fixed
- Hooks never break the session, and the launcher skips Python in projects without `sdd.toml`.
- `sdd start` finds the branch in a repository without commits.
- Concurrent sessions no longer overwrite each other's trust-store records (locked writes).
- Check steps have a timeout; state is written atomically; invalid regexes fail at load time.
- `init --specs-dir` is written to the generated `sdd.toml`.
- Task tables: `|` inside backticks, and a Status column that is not last.
- Briefs: flat package layouts, `async def`, syntax errors, size caps.

Thanks to GitHub's [Spec Kit](https://github.com/github/spec-kit) for the ideas behind clarify, analyze and the constitution.

## 0.1.0 - 2026-11-02 (as sdd-pilot)
- Plugin with planner, implementer, reviewer and clerk agents; `run`, `init` and `remote` skills.
- `sdd` CLI: gate, briefs, run state with caps, approvals log, hooks.
- `sdd.toml` configuration with python, node and generic presets.
- Phone approvals through the official Telegram channel plugin.
