---
name: implement-ticket
description: Drive the multi-agent ticket implementation flow for the claude-code-remote project — pick the next [todo] ticket from TICKETS.md, create the feature branch, dispatch team-lead → developer → qa+reviewer → team-lead, and open the PR. Invoke this when the user says "/implement-ticket", "work on CCR-N", "implement Phase N", "continue project work", or otherwise asks for ticket progress. Without this skill, the main session does NOT act as an orchestrator.
---

# implement-ticket

Run the per-ticket multi-agent loop end-to-end as the **orchestrator**. The main session is the only entity that dispatches subagents — subagents cannot invoke other subagents. The main session also owns all git operations (branch creation, commits, pushes, PRs).

## Roles

Six specialist subagents live in `.claude/agents/`:

- **`project-manager`** — slices plan phases or user ideas into tickets in `TICKETS.md`; creates feature folders under `.claude/docs/`. Cannot write code or run code. *Currently not used in this skill — there are enough tickets; PM will move into a future `/research` skill.*
- **`team-lead`** — plans and verdicts. Mode 1 (initial): produces dev/QA/reviewer scope briefs. Mode 2 (final): synthesizes QA + reviewer + dev report into APPROVED or a fix dispatch. Updates `TICKETS.md`, `CONTEXT.md`, `BRIEF.md`. Cannot write or run code.
- **`python-developer`** — Python application code: `src/ccr/{bot,auth,claude,console,db,events}/`, `cli.py`, `server.py`, `config.py`, `logging_setup.py`, `alembic/`, plus `install.sh`, `.github/workflows/`, `.env.example`, `.pre-commit-config.yaml`, the `doctor` subcommand, and corresponding tests. Writes code; reports a detailed work summary; does not edit `TICKETS.md` / `CONTEXT.md` / `BRIEF.md`; does not run git.
- **`web-developer`** — `src/ccr/web/` (FastAPI, SSE, proxy, viewer frontend) and its tests. Same constraints as python-developer.
- **`qa`** — runs the ticket's `Acceptance:` commands literally, runs targeted tests, runs lint + types, reports pass/fail and missing-test gaps. Cannot edit any project file.
- **`reviewer`** — security review of the diff: hardcoded secrets, env-var leaks, command/SQL injection, path traversal, JWT/crypto misuse, auth-boundary gaps. Cannot edit any project file.

There is no sysops agent. Install / CI / doctor work is python-developer's. There is no worktree isolation — developers work directly on the feature branch the main session creates.

The shared protocol — ticket schema, status transitions, BRIEF/CONTEXT format, handoff verdict strings, the per-ticket flow — lives in `.claude/docs/WORKFLOW.md`. Read it before invoking any agent.

## State files

- `TICKETS.md` — append-only ticket log; status lives in the title `## CCR-NNN: <title> [<status>]` for grep-ability. Statuses: `todo` `in-progress` `done` `blocked` (no `in-review`).
- `.claude/docs/<feature>/BRIEF.md` — short feature summary, written by team lead on feature completion.
- `.claude/docs/<feature>/CONTEXT.md` — file structure + change history per feature, written by team lead from the developer's per-ticket report.

## Flow

Run this loop in this session.

1. **Read state.** `.claude/docs/WORKFLOW.md` (protocol), `TICKETS.md` (status), the relevant phase section of `claude-code-remote-plan.md` (context).
2. **Pick the next ticket.** First `[todo]` whose `Depends on:` are all `[done]`. If the user named a specific ticket, use that one — but still verify its dependencies are satisfied.
3. **Check git state.** Run `git status`. If the working tree is dirty (uncommitted changes), **stop and ask the user** before doing anything else — do not auto-stash, do not auto-commit, do not create a branch on top of dirty state.
4. **Create the feature branch.** `git checkout -b ccr-NNN-<slug>` where the slug is a 1–4-word kebab-cased summary of the ticket title. Branch off `main`.
5. **Mark the ticket `[in-progress]`** in `TICKETS.md` and append a Review log line: `<YYYY-MM-DD> main: branch ccr-NNN-<slug> created, dispatching team-lead`.
6. **Dispatch `team-lead` (Mode 1).** Prompt: the ticket id and "produce initial scope brief". Team-lead returns three sections (dev scope, QA plan, reviewer focus) and a `DISPATCH: <python-developer|web-developer> CCR-NNN` verdict. Quote each section verbatim when dispatching the corresponding agent later.
7. **Dispatch the developer** that team-lead named. Pass the `## Developer scope (CCR-NNN)` section verbatim in the prompt. Developer writes code + tests, runs self-checks, returns a structured implementation summary and `READY FOR REVIEW: CCR-NNN`.
8. **Dispatch `qa` and `reviewer` in parallel.** One message, two Agent tool blocks. To `qa`: pass team-lead's `## QA test plan (CCR-NNN)` section + a one-paragraph quote of the developer's "Files created or modified" list so qa knows what changed. To `reviewer`: pass team-lead's `## Reviewer focus (CCR-NNN)` section + the same files-changed paragraph.
9. **Dispatch `team-lead` (Mode 2).** Pass the developer's full implementation summary, qa's full response, and reviewer's full response. Team-lead either:
   - Returns `APPROVED: CCR-NNN` (or `FEATURE COMPLETE: <feature>` for the feature's last ticket) — proceed to step 10.
   - Returns `DISPATCH: <agent> CCR-NNN` with a `## Fix scope (CCR-NNN)` section in the body — go back to step 7 with a **fresh developer dispatch**, passing the fix scope verbatim. The dev session is new every time; the fix scope must be self-contained.
10. **On APPROVED — pause for user approval.** This is a hard gate. **Do not** stage, commit, push, or run `gh pr create` until the user explicitly approves. End the turn by:
    - Posting a short summary: ticket id + title, branch name, files touched, QA + reviewer verdicts, proposed commit subject, and a preview of the PR title + body.
    - Asking the user to approve the publish, e.g. "Ready to commit, push `ccr-NNN-<slug>`, and open the PR?".
    - Stopping prompt execution. Wait for the user's reply. **Do not** continue to step 11 in the same turn.
11. **On user approval — create the PR.** Only after the user says go (e.g. "yes", "ship it", "approved"). Per `WORKFLOW.md §Integration`:
    - Stage the ticket's files plus the team-lead's `TICKETS.md` / `CONTEXT.md` / `BRIEF.md` updates.
    - Commit with the format from WORKFLOW.md.
    - `git push -u origin ccr-NNN-<slug>`.
    - `gh pr create --base main --title "CCR-NNN: <title>" --body "<filled template>"`.
    - Report the PR URL to the user.
    - **Stop.** Never `gh pr merge`, never push to `main`, never force-push. The user merges manually.

    If the user says no, asks for changes, or replies ambiguously, do not publish — address the request, then re-summarize and re-ask. A "yes" approves the current ticket only; subsequent tickets need their own approval.

## Boundary rules

- A ticket spanning `src/ccr/{bot|auth|...}` and `src/ccr/web/` must be split. If team-lead returns `BLOCKED: CCR-NNN — ticket spans web + python scope`, stop and tell the user — slicing tickets is the project-manager's job and lives in a separate flow.
- Multiple `[todo]` tickets can be dispatched in parallel only if **all** hold: different developer agents, no shared files, no `Depends on:` between them. Otherwise serialize. Parallel dispatches go in a single message with multiple Agent tool blocks. The per-ticket flow (TL → dev → QA+review → TL) is sequential within a ticket; parallelism is between independent tickets.
- `qa` and `reviewer` always run in parallel within a single ticket — they have no dependency on each other.

## Why this is a skill, not always-on behavior

The orchestrator role is heavyweight: it loads agent definitions into mental context, makes the session feel like it should always dispatch instead of answering directly, and is wrong for ad-hoc questions, debugging help, or one-off edits. Loading the flow only when the user explicitly asks for ticket work keeps the default session lean and direct.

A future `/research` skill will cover the project-manager-driven slicing flow (taking a phase or feature description and producing tickets). It is intentionally out of scope here.
