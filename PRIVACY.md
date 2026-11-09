# Privacy policy

specsteward is an open-source Claude Code plugin. It has no servers and no accounts, and
its author receives no data from it.

## What it collects

Nothing. There is no telemetry, analytics, crash reporting or usage tracking.

## What it stores, and where

Everything stays on your computer:

| What | Where |
|---|---|
| Run state, task briefs, review and gate results, approvals log | `.sdd/` in your project (git-ignored) |
| Trusted `sdd.toml` hashes, spec approvals, recorded task statuses | `~/.claude/specsteward/` |
| Phone bot token (only if you set up phone control) | Your operating system's credential store (Windows DPAPI, macOS Keychain, Linux Secret Service), or an owner-only file where there is none |
| Your own Telegram or Discord account ID, on the bot's allowlist (only if you set up phone control; the wizard shows your display name once so you can confirm it is you, and does not store it) | `~/.claude/channels/<channel>/access.json`, the official plugin's file |
| Specs, plans and tasks you write | `specs/` in your project, under your own version control |

Delete these folders, and the credential-store entry, to remove everything (see
[Uninstall](README.md#uninstall)).

## Network connections

specsteward itself connects to the internet only to:

- install **graphify** (pinned version) from PyPI, for the local code graph;
- install **Bun** (pinned version, checksum-verified) from its official release, only if you
  set up phone control;
- check the bot token you paste with Telegram's or Discord's own API during
  `sdd phone setup`, and send you one test message there.

## Services you choose

- **Claude Code and the Claude models** run the agents. What they send to Anthropic is
  governed by Anthropic's terms and privacy policy, as for any Claude Code session.
- **Phone control** uses Anthropic's official Telegram or Discord channel plugin. Messages
  you exchange with your bot (status lines, spec summaries, files you ask for) pass through
  Telegram or Discord under their privacy policies. specsteward is told never to send
  secrets or `.env` files to the chat; do not use phone control for specs you cannot share
  with that service.
- **GitHub**, if you ship with `gh`: commits, pushes and pull requests go where you send them,
  each behind your permission prompt.

## Contact

Questions about privacy: open an issue at
https://github.com/Dinusha-Ekanayake/specsteward/issues, or report a security problem
privately through [SECURITY.md](SECURITY.md).

Last updated: 2026-11-10.
