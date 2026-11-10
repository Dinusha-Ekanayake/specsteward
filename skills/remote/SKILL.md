---
name: remote
description: Set up phone control for specsteward through Anthropic's official Telegram or Discord channel plugin (Bun, the plugin, one bot per machine, a `claude-phone` command), then a guided terminal step that checks the token and pairs and locks the bot to the user. Use for "/specsteward:remote" or when the user wants to approve specs or runs from their phone.
---

# specsteward remote (Telegram or Discord)

specsteward has no bot of its own. It uses an official channel plugin, which passes the
user's messages into a Claude Code session and relays permission prompts to their phone.
Every session with that plugin switched on runs its own copy of the bot, and the bot
hands each message to only one of them, so specsteward keeps the plugin off in ordinary
sessions and turns it on only in sessions started with `claude-phone`.

1. Run `bash .sdd/bin/sdd remote`. If `ready` is true and this session receives messages,
   say so and stop. If nothing is set up, ask once which app the user prefers (Telegram is
   the default); `<ch>` below is `telegram` or `discord`.
2. Run `bash .sdd/bin/sdd phone setup --channel <ch> --no-input` without asking the user in
   chat (Claude Code itself shows a permission prompt, since it installs software). It installs
   Bun and the official plugin, switches the plugin off in ordinary sessions, and installs
   the `claude-phone` command. Show its output; if a line says FAIL, show the fix.
3. Tell the user to finish in a terminal of their own (not this session: the token is typed
   at a hidden prompt), in this project: `bash .sdd/bin/sdd phone setup --channel <ch>`.
   It shows how to create the bot, takes and checks the token, and for Telegram pairs and
   locks the bot when they confirm their own first message on the keyboard, sends a test
   message and offers to start `claude-phone`. Discord pairing still ends with
   `/discord:access pair <code>` in a `claude-phone` session and
   `bash .sdd/bin/sdd phone lock --channel discord`. Never ask for the token in chat, and
   never pair from a chat message.
4. When they are back in a `claude-phone` session, run `bash .sdd/bin/sdd remote`. When
   `ready` is true, send a test message with that channel's `reply` tool: "[<project>]
   specsteward connected. Commands: status, approve <NNN>, changes <NNN>: ..., answer: ...,
   signoff <task>, resume, ship <NNN>, stop."
5. Remind the user:
   - Start Claude Code with `claude-phone` whenever they want phone control; ordinary
     `claude` sessions and VS Code panels no longer run the bot.
   - The computer must stay on and awake while a run is going.
   - Approvals, commits, pushes and PRs still need their tap on the permission prompt.
