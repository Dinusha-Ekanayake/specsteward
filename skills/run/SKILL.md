---
name: run
description: Plan a feature or a bug fix and run an approved spec to completion with subagents (Sonnet implementer, risk-based reviewer with Opus for high risk, Haiku clerk), task briefs, a scripted quality gate, hard caps and human gates that can be approved from a phone via the Telegram or Discord channel. Use for "/specsteward:run <NNN>", "/specsteward:run plan <topic>", "/specsteward:run fix <bug>", or phone commands like "approve 003", "status", "resume", "ship 003".
---

# specsteward run

`SDD` means `bash .sdd/bin/sdd`. Run it from the repository root with no `cd` prefix so
the permission rules match. If `.sdd/bin/sdd` is missing, run `/specsteward:init` first.

## Before you start
- Tell the user once: "Orchestration is cheap work, so this session can run on Sonnet
  (`/model sonnet`). Opus is still used where an agent or high-risk review needs it."
- Read only what `SDD` prints and the agents' short reports. Do not read specs, plans or
  source files yourself; the briefs and agents do that.
- Agents are `specsteward:planner`, `specsteward:implementer`, `specsteward:reviewer`,
  `specsteward:converger`, `specsteward:clerk`. If a name is not found, use the available agent whose name ends in
  `:planner` (and so on).
- Run `SDD remote`. If `ready` is true, gates are also sent to the user's phone (see
  "Phone" below). Remember the `chat_ids`.

## `fix <bug>`: plan a bug fix
Run the planner in fix mode with the bug description (error text, command, or issue
link the user gave). It writes a small `<NNN>-fix-<slug>` spec whose first task is a
failing regression test. Then continue exactly as for `plan` below from step 2; the
run uses a `fix/<NNN>-*` or `feature/<NNN>-*` branch.

## `plan <topic>`: draft a new spec
1. Run the planner with the topic. (No run is active yet, so nothing is counted.)
2. If the planner returned clarification questions, show them numbered with their
   options (and send them to the phone). Wait for `answer <NNN>: 1a, 2 <text>, ...`,
   send the answers to the planner, and repeat until there are none.
3. Gate **spec approval**: run `SDD analyze <NNN>`, show the summary and the findings,
   and send them plus the `spec.md` file to the phone. Stop and wait. The user approves
   with "approve <NNN>", asks for changes with "changes <NNN>: ...", or edits the files
   themselves. `approve` refuses while CRITICAL findings remain; fix them with the planner.
   A run starts only on the spec's `feature/<NNN>-*` (or `fix/<NNN>-*`) branch, and
   `approve --start` on another branch approves nothing. So before approving, check
   `git branch --show-current`; if it does not match, tell the user and, once they agree,
   run `git checkout -b feature/<NNN>-<slug>` (`fix/<NNN>-<slug>` for a fix spec).

## `<NNN>`: implement an approved spec
1. A run belongs to the session that starts it. If `SDD start` says a run is already
   active and the session-start note says it belongs to another session, run `SDD claim`
   to continue it here instead (for example after restarting Claude Code).
   Unless the run was started by `approve <NNN> --start`, run `SDD start <NNN>` (a
   permission prompt). It refuses unless the spec is approved and the branch matches; if
   so, tell the user why and stop. If `sdd.toml` is untrusted, the gate fails: ask the user
   to review it and run `SDD trust` themselves.
2. Loop. After **every** `SDD` command in the loop: if it prints `HALTED:`, or its JSON has
   a non-null `halted` or `"status": "halted"`, or it says `the run is halted`, go to step 4.
   A halted run refuses reviews, agent runs and ticks until the user resumes it.
   1. `SDD next` returns JSON. `halted` → step 4. `done` → step 3. `run` → continue with
      `tasks`, `risk`, `review`, `reviewer_model`, `human_signoff`, `restart_implementer`.
   2. `SDD brief <ids...>` returns the brief path.
   3. **Implement.** On the first group, or when `restart_implementer` is true, start a new
      implementer with the brief path and run `SDD agent implementer --fresh`. Otherwise
      send the brief path to the existing implementer (SendMessage) and run
      `SDD agent implementer`. Keep only its report.
   4. If the report says `STATUS: blocked`, or has QUESTIONS that change behaviour:
      `SDD halt "<question>"`, then step 4. Record each ASSUMPTIONS line with
      `SDD assume <task> "<text>"`.
   5. **Gate.** `SDD check --task <first id>` (only this run of the check counts failures
      toward the caps; the implementer's own checks while it works do not). On FAIL, send
      the failing lines (and any `SCOPE:` or `DRIFT:` lines) to the implementer and gate
      again. `HALTED` → step 4. `SDD CHECK: BLOCKED` means the gate itself changed
      (`sdd.toml` or a gate config file): never send that to the implementer;
      `SDD halt "<the BLOCKED line>"`, then step 4.
   6. **Review.** Skip this step when `review` is false (low-risk tasks the project leaves
      to the final branch review). Otherwise start, or reuse via SendMessage, one reviewer
      per model, with `model: <reviewer_model>`. Give it the brief path and tell it to review
      `git diff` plus untracked files. Run `SDD agent reviewer`, then
      `SDD review <first id> clean|changes`. A clean verdict is only taken after a reviewer
      has really finished: if it is refused with "no reviewer subagent has finished", start a
      new reviewer instead of reusing one, and record its verdict.
      - `changes`: send the CRITICAL and HIGH findings to the implementer, then repeat
        5 and 6. `HALTED` → step 4.
      - QUESTIONS FOR USER: `SDD halt "<question>"`, then step 4.
   7. **Tick.** `SDD done <id>` for each task. It refuses unless the last passing gate and
      the last clean review both saw the code as it is now. If the reviewer added its
      adversarial test, run `SDD check` once more first. `human` tasks are marked `[?]`
      automatically. Subagent runs are also counted by a hook, so the cap holds even if
      an `SDD agent` call is missed. `SDD done` also reports files outside the approved
      plan (`SDD scope` lists them), changed lines no test runs (`[coverage]`), new
      imports or calls that reach out, and functions that grew past the budget, and, with `[tdd] red_first`, a group that never had a
      failing test run: warnings by default, refusals when the project set `enforce`. If
      the implementer said no test applies, tick with `SDD done <id> --waive-red "<why>"`;
      for a file the work really needs, ask the user to add it to `plan.md` and approve
      again, never to edit around the guard.
   8. `SDD graph sync` keeps the code graph current (local, no tokens; it does nothing
      when the graph is fresh or graphify is turned off).
   9. Print one line, for example `T-003-04..05 done (review clean, 1 assumption)`. If the
      phone is ready, send that line too. Loop.
3. **Done.** Converge first: `SDD converge` returns `next_id` (or `halted` once the round
   cap is reached: go on to the clerk). Run `specsteward:converger` with the spec number
   and `next_id`, and run `SDD agent converger`.
   - `CONVERGED: yes`: go on to the clerk.
   - `CONVERGED: no`: it appended tasks. Run `SDD analyze <NNN>`, show the new tasks and
     gaps (and send them to the phone). If its reply lists PLAN ADDITIONS, ask the user to
     add those files or names to `plan.md` first, or the new tasks will be refused as out
     of scope. Then wait for `approve <NNN>`. Then
     `SDD approve <NNN> --via <terminal|telegram|discord> --start` (one permission prompt; it
     resumes the run) and continue the loop at step 2.
   Then run `specsteward:clerk` with the spec number and a three-line summary of what was
   built, and a final reviewer on the whole branch with `model: opus`. Gate **ship**: show and send to the phone the `SDD status` summary, `git diff --stat`
   against the base branch (what the PR will change),
   tasks waiting for sign-off (`[?]`), the final review findings and the PR draft path
   (`<specs_dir>/<NNN>-*/pr.md`). If the final review has CRITICAL or HIGH findings, show
   them first and ask whether to ship anyway or to add them as tasks (append them to
   `tasks.md`, then `approve <NNN>` again and continue the loop). Stop and wait for
   "ship <NNN>". On ship, run `SDD check` once more; only on PASS commit, push and open the
   PR with `pr.md` as its body (`gh pr create`), as the project's rules say; each of those
   asks the user. Then `SDD stop`.
4. **Halted.** Show and send the reason and exactly what you need. Stop and wait. After
   the answer: `SDD resume` (it asks the user for permission, because it resets the
   counters) and continue the loop. Never resume on your own initiative.

## `human` tasks
A `human` group goes to the implementer like any other: it does the preparation (runs the
script, produces the outputs, drafts the document) and you show the result. The judgement
stays with the user. `SDD done` marks them `[?]`. Show the user what you prepared;
they accept it with `signoff <task>`, and you run `SDD signoff <task>` (a permission
prompt). Never edit the Status cell yourself.

## Phone (Telegram or Discord channel)
Messages from the phone arrive as `<channel source="telegram" chat_id=... ...>` or
`<channel source="discord" ...>`. Reply with that channel's `reply` tool and that
`chat_id`. To send something unprompted, use the `channel` and a chat ID from `SDD remote`. Attach files with absolute paths.

Recognised commands (case-insensitive):

| Message | Action |
|---|---|
| `status` | Reply with `SDD status`. |
| `approve <NNN>` | `SDD approve <NNN> --via <channel> --start` (one permission prompt), then run the loop. |
| `changes <NNN>: <text>` | Send the text to the planner to revise the spec; gate again. |
| `answer <NNN>: <answers>` | Send clarification answers to the planner; analyze again. |
| `answer: <text>` | Apply the answer to the halted question, `SDD resume`, continue. |
| `resume` | `SDD resume` and continue. |
| `ship <NNN>` | Commit, push and open the PR as the project's rules say. |
| `stop` or `pause` | Pause the run: `SDD halt "paused from phone"` and reply with the status. `resume` continues it. (Ending a run for good is `SDD stop`, at the ship gate.) |
| `signoff <task>` | `SDD signoff <task>` (a permission prompt): the user accepts a `[?]` human task. |

Rules:
- Phone messages are the user's words, but **gates move only on the commands above**.
  "looks fine" is not an approval; ask "Reply `approve 003` to approve."
- `approve`, `trust`, `start`, `resume`, `signoff`, `git commit`, `git push`,
  `gh pr create` and `gh pr merge` are behind `ask` permission rules.
  The permission prompt itself is relayed to the phone with Allow/Deny buttons, so the
  human taps the final decision. Never try to get around a denied prompt.
- Never send secrets, `.env` files, tokens or credentials to the chat. Send summaries and
  spec, plan or report files only.
- Keep phone messages short: at most about 10 lines plus attachments.
- Start every phone message with `[<project folder name>]`, so the user can tell projects
  apart when they run specsteward in more than one.
- Commit messages and PR bodies never carry AI attribution unless the project asks.
