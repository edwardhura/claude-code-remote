---
name: sync-claude-session-with-remote
description: Register a local Claude Code session with the Claude Code Remote (CCR) bot so it can be resumed via /continue from Telegram.
---

# Sync local Claude session with CCR

Register the **current** Claude Code session (or one given by id) with the
running Claude Code Remote bot so that you can resume it from Telegram via
`/continue` or `/continue <prefix>`.

## When to use

Run this skill when:

- You started a Claude Code session locally (not through CCR's `/new`) and
  realised mid-session that you want it accessible from your phone.
- You want to bring a finished or stopped local session into CCR's
  `/sessions` listing so it survives a restart of the bot.

## What it does

1. Determines the Claude session id to import:
   - If the user supplies an explicit id (a UUID Claude logged in its
     `system.init` event), use that.
   - Otherwise, pick the most-recently-modified `*.jsonl` under
     `~/.claude/projects/<encoded-cwd>/` and use its filename (without the
     `.jsonl` extension) as the session id.
2. Invokes:

   ```bash
   python -m ccr session save <claude_session_id>
   ```

   from the CCR project root (the directory containing this repo's
   `pyproject.toml`).
3. Surfaces the resulting CCR row prefix and a reminder that the user can
   now find the session in `/sessions` and resume it via
   `/continue <prefix>` from the Telegram bot.

## Notes

- This skill does NOT copy Claude's JSONL into CCR's `data/logs/`. Claude
  owns its history; CCR only stores metadata and resumes via
  `claude --resume <session_id>`.
- If CCR has no registered owner, the import will fail with
  `"No owner registered. Pair the owner first."` — the user should run
  `python -m ccr console` and pair their Telegram account before re-running
  this skill.
- The same `<claude_session_id>` cannot be imported twice; a second attempt
  exits non-zero with `"Already imported: <id>"`.
