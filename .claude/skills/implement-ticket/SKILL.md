---
name: implement-ticket
description: Drive the multi-agent ticket implementation flow for the claude-code-remote project — pick the next [todo] ticket from BACKLOG.md, create the feature branch, dispatch team-lead → (architect → team-lead) → developer → reviewer → team-lead, and open the PR. Invoke this when the user says "/implement-ticket", "work on CCR-N", "implement Phase N", "continue project work", or otherwise asks for ticket progress. Without this skill, the main session does NOT act as an orchestrator.
---

# implement-ticket

Run the per-ticket multi-agent loop end-to-end as the **orchestrator**. The main session is the only entity that dispatches subagents — subagents cannot invoke other subagents. The main session also owns all git operations (branch creation, commits, pushes, PRs).

## Roles

Six specialist subagents live in `.claude/agents/`:

- **`project-manager`** — slices plan phases or user ideas into tickets appended to `BACKLOG.md`; creates feature folders under `.claude/docs/`. Cannot write code or run code. *Currently not used in this skill — there are enough tickets; PM will move into a future `/research` skill.*
- **`team-lead`** — plans and verdicts. Mode 1A (start of ticket): decides whether to dispatch the architect first or skip straight to a developer brief. Mode 1B (post-architect): composes the developer brief from `.claude/plans/CCR-NNN-<slug>.md`. Mode 2 (final): synthesizes the reviewer's report + dev report into APPROVED or a fix dispatch. Updates `BACKLOG.md` / `DONE.md` (including moving the ticket entry from BACKLOG.md to DONE.md on `done`), `CONTEXT.md`, `BRIEF.md`. Cannot write or run code.
- **`architect`** — design pass: reads the codebase + ticket, decides patterns / file placement / abstractions, writes a plan file at `.claude/plans/CCR-NNN-<slug>.md`. May include short illustrative code sketches but writes no production code or tests. Runs only when team-lead Mode 1A asks.
- **`python-developer`** — Python application code: `src/ccr/{bot,auth,claude,console,db,events}/`, `cli.py`, `server.py`, `config.py`, `logging_setup.py`, `alembic/`, plus `install.sh`, `.github/workflows/`, `.env.example`, `.pre-commit-config.yaml`, the `doctor` subcommand, and corresponding tests. Writes code; reports a detailed work summary; does not edit `BACKLOG.md` / `DONE.md` / `CONTEXT.md` / `BRIEF.md`; does not run git.
- **`web-developer`** — `src/ccr/web/` (FastAPI, SSE, proxy, viewer frontend) and its tests. Same constraints as python-developer.
- **`reviewer`** — code review + security review + test-coverage audit + green-suite confirmation. Reads the diff, judges design, scans for hardcoded secrets / env-var leaks / command-and-SQL injection / path traversal / JWT-and-crypto misuse / auth-boundary gaps, then runs the ticket's acceptance commands + targeted `pytest` + `ruff` + `mypy` at the end of its pass. Cannot edit any project file. *No separate QA agent — reviewer runs the test suite as the last step of its pass.*

There is no sysops agent. Install / CI / doctor work is python-developer's. There is no worktree isolation — developers work directly on the feature branch the main session creates. The legacy `qa` agent file is still on disk but is not dispatched in this flow.

The shared protocol — ticket schema, status transitions, BRIEF/CONTEXT format, handoff verdict strings, the per-ticket flow — lives in `.claude/docs/WORKFLOW.md`. Read it before invoking any agent.

## State files

- `BACKLOG.md` — active tickets in statuses `todo` / `in-progress` / `blocked`. Status lives in the title `## CCR-NNN: <title> [<status>]` for grep-ability. Append-only — never delete content.
- `DONE.md` — archived tickets in statuses `done` / `closed`. Entries arrive here by being **moved** verbatim from `BACKLOG.md` when a ticket reaches a terminal state (team-lead on `done`, main session on `closed`).
- `.claude/docs/<feature>/BRIEF.md` — short feature summary, written by team lead on feature completion.
- `.claude/docs/<feature>/CONTEXT.md` — file structure + change history per feature, written by team lead from the developer's per-ticket report.
- `.claude/plans/CCR-NNN-<slug>.md` — per-ticket design plan, written by the architect when team-lead Mode 1A asks. The developer reads it and follows it; the reviewer reads it and flags undocumented deviations.

Status set: `todo` `in-progress` `done` `blocked` `closed`. No `in-review`. `done` and `closed` are the terminal states that live in `DONE.md`; everything else lives in `BACKLOG.md`.

## Flow

Run this loop in this session.

1. **Read state.** `.claude/docs/WORKFLOW.md` (protocol), `BACKLOG.md` (active queue), `DONE.md` only when looking up history of a finished ticket, the relevant phase section of `claude-code-remote-plan.md` (context).
2. **Pick the next ticket.** First `[todo]` in `BACKLOG.md` whose `Depends on:` are all `[done]` (look in `DONE.md` to verify dependencies). If the user named a specific ticket, use that one — but still verify its dependencies are satisfied.
3. **Check git state.** Run `git status`. If the working tree is dirty (uncommitted changes), **stop and ask the user** before doing anything else — do not auto-stash, do not auto-commit, do not create a branch on top of dirty state.
4. **Create the feature branch.** `git checkout -b ccr-NNN-<slug>` where the slug is a 1–4-word kebab-cased summary of the ticket title. Branch off `main`. Remember `<slug>` — the architect's plan file and the PR will reuse it.
5. **Mark the ticket `[in-progress]`** in `BACKLOG.md` (entry stays in `BACKLOG.md`) and append a Review log line: `<YYYY-MM-DD> main: branch ccr-NNN-<slug> created, dispatching team-lead`.
6. **Dispatch `team-lead` (Mode 1A — architect-or-dev decision).** Prompt: the ticket id and "produce initial scope brief". Team-lead returns one of:
   - `DISPATCH: architect CCR-NNN` with `## Architect brief (CCR-NNN)` and `## Why architect (CCR-NNN)` sections → go to step 7.
   - `DISPATCH: <python-developer|web-developer> CCR-NNN` with `## Developer scope (CCR-NNN)` and `## Reviewer focus (CCR-NNN)` sections → skip to step 9.
7. **(Architect path only) Dispatch `architect`.** Pass the `## Architect brief (CCR-NNN)` section verbatim and tell the architect the slug so it writes to the right path. Architect reads the codebase, writes `.claude/plans/CCR-NNN-<slug>.md`, and returns `PLAN READY: CCR-NNN` with a short executive summary. If it returns `BLOCKED: CCR-NNN — <reason>`, surface the reason to the user and stop — do not proceed.
8. **(Architect path only) Dispatch `team-lead` (Mode 1B — compose dev brief).** Pass the architect's full response and the plan file path. Team-lead reads `.claude/plans/CCR-NNN-<slug>.md` in full and returns `DISPATCH: <python-developer|web-developer> CCR-NNN` with `## Developer scope (CCR-NNN)`, `## Plan reference (CCR-NNN)`, and `## Reviewer focus (CCR-NNN)` sections.
9. **Dispatch the developer** that team-lead named. Pass the `## Developer scope (CCR-NNN)` section verbatim. If team-lead emitted a `## Plan reference (CCR-NNN)` block (architect path), include that too — the dev should open and follow `.claude/plans/CCR-NNN-<slug>.md`. Developer writes code + tests, runs self-checks, returns a structured implementation summary and `READY FOR REVIEW: CCR-NNN`.
10. **Dispatch `reviewer` (solo).** Pass team-lead's `## Reviewer focus (CCR-NNN)` section + a one-paragraph quote of the developer's "Files created or modified" list. If a plan file exists, point the reviewer at it so deviations get caught. The reviewer does code review + security checks + coverage audit, then runs the ticket's acceptance commands + targeted `pytest` + `ruff` + `mypy` at the end of its pass, and returns `REVIEW PASS: CCR-NNN` or `REVIEW FAIL: CCR-NNN — <summary>`.
11. **Dispatch `team-lead` (Mode 2 — verdict).** Pass the developer's full implementation summary and the reviewer's full response. Team-lead either:
    - Returns `APPROVED: CCR-NNN` (or `FEATURE COMPLETE: <feature>` for the feature's last ticket) — team-lead has already moved the ticket entry from `BACKLOG.md` to `DONE.md`, ticked acceptance boxes, and updated CONTEXT/BRIEF. Proceed to step 12.
    - Returns `DISPATCH: <agent> CCR-NNN` with a `## Fix scope (CCR-NNN)` section in the body — ticket stays `[in-progress]` in `BACKLOG.md`. Go back to step 9 with a **fresh developer dispatch**, passing the fix scope verbatim. The dev session is new every time; the fix scope must be self-contained.
12. **On APPROVED — pause for user approval.** This is a hard gate. **Do not** stage, commit, push, or run `gh pr create` until the user explicitly approves. End the turn by:
    - Posting a short summary: ticket id + title, branch name, files touched, reviewer verdict, architect plan path (if any), proposed commit subject, and a preview of the PR title + body.
    - Asking the user to approve the publish, e.g. "Ready to commit, push `ccr-NNN-<slug>`, and open the PR?".
    - Stopping prompt execution. Wait for the user's reply. **Do not** continue to step 13 in the same turn.
13. **On user approval — create the PR.** Only after the user says go (e.g. "yes", "ship it", "approved"). Per `WORKFLOW.md §Integration`:
    - Stage the ticket's files plus the team-lead's `BACKLOG.md` / `DONE.md` / `CONTEXT.md` / `BRIEF.md` updates and the architect's `.claude/plans/CCR-NNN-<slug>.md` if it was created. Both `BACKLOG.md` (the cut) and `DONE.md` (the paste) are part of the same commit.
    - Commit with the format from WORKFLOW.md.
    - `git push -u origin ccr-NNN-<slug>`.
    - `gh pr create --base main --title "CCR-NNN: <title>" --body "<filled template>"`.
    - Report the PR URL to the user.
    - **Stop.** Never `gh pr merge`, never push to `main`, never force-push. The user merges manually.

    If the user says no, asks for changes, or replies ambiguously, do not publish — address the request, then re-summarize and re-ask. A "yes" approves the current ticket only; subsequent tickets need their own approval.

### Closing a ticket without acceptance verification

If the user decides a ticket should be abandoned, rejected, or auto-closed (e.g. superseded by another ticket, scope no longer relevant), the **main session** handles the close:

1. In `BACKLOG.md`, flip the title status to `[closed]`.
2. Append a Review log line: `<YYYY-MM-DD> main: closed — <one-line reason>`.
3. Move the ticket entry from `BACKLOG.md` to `DONE.md` per `WORKFLOW.md §How to move a ticket`.
4. Commit the move with a short message; no PR / no acceptance work.

Do not close a ticket on your own initiative — only on explicit user direction.

## Boundary rules

- A ticket spanning `src/ccr/{bot|auth|...}` and `src/ccr/web/` must be split. If team-lead returns `BLOCKED: CCR-NNN — ticket spans web + python scope`, stop and tell the user — slicing tickets is the project-manager's job and lives in a separate flow.
- Multiple `[todo]` tickets can be dispatched in parallel only if **all** hold: different developer agents, no shared files, no `Depends on:` between them. Otherwise serialize. Parallel dispatches go in a single message with multiple Agent tool blocks. The per-ticket flow (TL → optional architect → TL → dev → reviewer → TL) is sequential within a ticket; parallelism is between independent tickets.
- The reviewer runs **solo** — there is no QA agent and nothing else runs in parallel with the reviewer. Test execution lives at the end of the reviewer's pass.
- Architect dispatch is **optional and team-lead-decided**. Do not dispatch the architect on your own initiative — let team-lead Mode 1A pick the path. Small additive tickets skip the architect entirely.

## Why this is a skill, not always-on behavior

The orchestrator role is heavyweight: it loads agent definitions into mental context, makes the session feel like it should always dispatch instead of answering directly, and is wrong for ad-hoc questions, debugging help, or one-off edits. Loading the flow only when the user explicitly asks for ticket work keeps the default session lean and direct.

A future `/research` skill will cover the project-manager-driven slicing flow (taking a phase or feature description and producing tickets). It is intentionally out of scope here.
