# How it works

## Pieces

| Piece | Role |
|---|---|
| `agents/planner.md` (Opus) | Drafts spec, plan, tasks. Never approves |
| `agents/implementer.md` (Sonnet) | TDD for one task group from a brief; reused across groups, restarted every few tasks |
| `agents/reviewer.md` (Sonnet or Opus) | Reviews the diff against the brief; Opus plus an adversarial test on high-risk tasks |
| `agents/converger.md` (Opus) | After the last task: compares the code with the spec, plan and constitution and appends the missing work as new tasks (append-only) |
| `agents/clerk.md` (Haiku) | Changelog, dev log and PR draft |
| `skills/run` | The orchestration loop and the phone command table |
| `skills/init`, `skills/remote` | Setup helpers |
| `specsteward/` | The deterministic part, as a standard-library Python CLI |
| `hooks/hooks.json` | SessionStart (shim, context, background graph sync), PreToolUse (guard `.sdd/` and the trust store), PostToolUse (format), SubagentStop (count agents), Stop (continue) |

Everything that can be a script is a script, so models spend tokens only on judgement: the CLI parses tasks, builds briefs, runs the gate, counts failures, enforces caps, keeps the code graph fresh and renders the status line and dashboard.

## The loop

```
sdd approve 003 --start  (prompt) records the spec's hash, then starts the run:
                         refuses unless approved content matches and the branch matches
loop:
  sdd next               -> {tasks, risk, review, reviewer_model, restart_implementer}
                            | halted | done   (records the group in flight)
  sdd brief T-003-01 ... -> .sdd/briefs/T-003-01.md
  implementer(brief)     -> short report
  sdd check              -> PASS (records the tree it saw) | FAIL + signature
                            (same signature twice halts; config changes fail `guard`)
  reviewer(brief)        -> VERDICT clean|changes (second open round halts)
  sdd review ... clean   -> records the tree the review saw
  sdd done T-003-01 ...  -> ticks tasks.md only if gate and review saw the current tree
                            ([?] for human tasks)
done: sdd converge -> converger appends missing work? -> you approve it (approve --start
      resumes the run) and the loop goes on; capped by max_converge_rounds
      then clerk, final Opus review of the branch, wait for "ship"
```

## Stop hook

While a run is active and tasks are open, the Stop hook asks Claude to continue, but only if a task was completed since the last automatic continue. No progress halts the run. A halted or stopped run never blocks a stop.

## State

`.sdd/state.json` (git-ignored, written atomically) holds the run: spec, halt reason, agent-run counts, the group in flight with the tree fingerprints its gate and review saw, per-task failures, review rounds and assumptions. `.sdd/briefs/` holds briefs, `.sdd/approvals.log` the approvals, `.sdd/dashboard.html` a read-only view regenerated on every change, `.sdd/bin/sdd` a stable shim to the installed plugin version so permission rules do not change across plugin updates.

Outside the repository, `~/.claude/specsteward/trust.json` holds what you trusted and approved (per repository, so git worktrees share it; run ids, ticks and the gate baseline are per worktree): the hash of `sdd.toml`, of each approved `spec.md`, and of the gate config files at the start of a run. Agents working in the project cannot edit it.

## Briefs

A brief contains the task rows; the lines of `plan.md` that mention the same backticked paths or identifiers (plain words are ignored so unrelated rows do not match); the `REQ-` and `AC-` lines of the spec; signatures of the files the plan names, plus the siblings of any new file so the implementer reuses what exists; and related graphify nodes. Each part is capped and marks what it cut. The rules and report format live in the implementer's agent prompt, which is cached for the run, instead of in every brief.

## Design choices

- **No custom bot.** The official channel plugin already handles pairing, allowlists and permission relay.
- **Standard library only.** Nothing to install; Python 3.11+ is the only requirement.
- **Gates are permission prompts**, not chat text, so they cannot be satisfied by a model reading a message.
- **Hooks no-op without `sdd.toml`,** and the launcher exits before starting Python, so a global install costs nothing elsewhere. Hook errors go to stderr and never break the session.
- **The code graph is on by default.** Agents query `graphify-out/` instead of reading files; the graph is rebuilt in the background when it is missing or stale, including on a fresh clone on another machine.
- **Read-only views.** The status line and dashboard come from the CLI, cost no tokens, and cannot approve or change anything.
