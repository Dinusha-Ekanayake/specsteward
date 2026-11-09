---
name: init
description: Set up specsteward in the current repository - detects the stack and the project's own lint, type and test commands, writes sdd.toml, spec/plan/tasks templates, the .gitignore entry and permission rules (approve, trust, start, resume, signoff, commit, push, PR create and PR merge stay behind "ask"), then checks the setup with `sdd doctor`. Use for "/specsteward:init".
---

# specsteward init

1. Run the launcher from the plugin (two levels above this skill's base directory):
   `bash "<plugin root>/scripts/sdd.sh" init`
   It detects the stack (Python, Node, Go, Rust, or a Makefile) and builds the gate from
   what the project already uses. It never overwrites existing files without `--force`.
   Pass `--preset <name>` only if the user asks for a specific one.
2. If the init output says "removed project hooks", tell the user the plugin now supplies
   those hooks and the project copies were duplicates (they ran every hook twice).
   Run `bash .sdd/bin/sdd doctor` and fix every `FAIL` line with the user, using the fix
   it prints. If `sdd.toml` already existed, the user must review it and run
   `bash .sdd/bin/sdd trust` themselves (it is a permission prompt).
3. Run `bash .sdd/bin/sdd check` once. If a step fails because of the project (not the
   setup), tell the user; that is the baseline the runs start from.
4. Tell the user, in at most five lines, what was detected and the next steps:
   - `/specsteward:run plan <topic>` to draft a spec.
   - `/specsteward:remote` to approve gates from a phone (optional).
   - Restart Claude Code once so the new permission rules load.
