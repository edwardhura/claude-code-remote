# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository state

This repo is **pre-implementation**. The only tracked source-of-truth file is `claude-code-remote-plan.md` — a 1200-line architecture & phased implementation plan. There is no `src/`, `pyproject.toml`, tests, or `.env.example` yet. Treat the plan as the authoritative spec; when starting work, read the relevant phase section first and follow the file paths, schemas, and acceptance criteria it prescribes.

When the user asks to "implement Phase N" or to add a feature, the plan's Section 8 (Implementation Phases) defines the file list, packages, tasks, and acceptance criteria for that phase. Do not invent a different layout — the phases are designed to build on each other.

## Multi-agent workflow

Implementation is driven by 6 specialist subagents in `.claude/agents/`. The **main session (this Claude) is the orchestrator** — it owns routing, branch creation, commits, pushes, and PRs. Subagents cannot invoke other subagents.

- **`project-manager`** — slices plan phases or user ideas into tickets in `TICKETS.md`; creates feature folders under `.claude/docs/`; cannot write code or run code.
- **`team-lead`** — plans and verdicts. Mode 1 (initial): produces dev/QA/reviewer scope briefs. Mode 2 (final): synthesizes QA + reviewer + dev report into APPROVED or a fix dispatch. Updates `TICKETS.md`, `CONTEXT.md`, `BRIEF.md`. Cannot write or run code.
- **`python-developer`** — Python application code: `src/ccr/{bot,auth,claude,console,db,events}/`, `cli.py`, `server.py`, `config.py`, `logging_setup.py`, `alembic/`, plus `install.sh`, `.github/workflows/`, `.env.example`, `.pre-commit-config.yaml`, the `doctor` subcommand, and corresponding tests. Writes code; reports a detailed work summary; does not edit `TICKETS.md` / `CONTEXT.md` / `BRIEF.md`; does not run git.
- **`web-developer`** — `src/ccr/web/` (FastAPI, SSE, proxy, viewer frontend) and its tests. Same constraints as python-developer.
- **`qa`** — runs the ticket's `Acceptance:` commands literally, runs targeted tests, runs lint + types, reports pass/fail and missing-test gaps. Cannot edit any project file.
- **`reviewer`** — security review of the diff: hardcoded secrets, env-var leaks, command/SQL injection, path traversal, JWT/crypto misuse, auth-boundary gaps. Cannot edit any project file.

There is no sysops agent. Install / CI / doctor work is python-developer's. There is no worktree isolation — developers work directly on the feature branch the main session creates.

The shared protocol — ticket schema, status transitions, BRIEF/CONTEXT format, handoff verdict strings, the per-ticket flow — lives in `.claude/docs/WORKFLOW.md`. Read it before invoking any agent.

State files:
- `TICKETS.md` — append-only ticket log; status lives in the title `## CCR-NNN: <title> [<status>]` for grep-ability. Statuses: `todo` `in-progress` `done` `blocked` (no `in-review`).
- `.claude/docs/<feature>/BRIEF.md` — short feature summary, written by team lead on feature completion.
- `.claude/docs/<feature>/CONTEXT.md` — file structure + change history per feature, written by team lead from the developer's per-ticket report.

## Routing rules (main session)

When the user says "implement Phase N", "work on CCR-N", "continue project work", or otherwise asks for progress, run this loop in this session.

1. **Read state.** `.claude/docs/WORKFLOW.md` (protocol), `TICKETS.md` (status), the relevant phase section of `claude-code-remote-plan.md` (context).
2. **Pick the next ticket.** First `[todo]` whose `Depends on:` are all `[done]`.
3. **Check git state.** Run `git status`. If the working tree is dirty (uncommitted changes), **stop and ask the user** before doing anything else — do not auto-stash, do not auto-commit, do not create a branch on top of dirty state.
4. **Create the feature branch.** `git checkout -b ccr-NNN-<slug>` where the slug is a 1–4-word kebab-cased summary of the ticket title. Branch off `main`.
5. **Mark the ticket `[in-progress]`** in `TICKETS.md` and append a Review log line: `<YYYY-MM-DD> main: branch ccr-NNN-<slug> created, dispatching team-lead`.
6. **Dispatch `team-lead` (Mode 1).** Prompt: the ticket id and "produce initial scope brief". Team-lead returns three sections (dev scope, QA plan, reviewer focus) and a `DISPATCH: <python-developer|web-developer> CCR-NNN` verdict. Quote each section verbatim when dispatching the corresponding agent later.
7. **Dispatch the developer** that team-lead named. Pass the `## Developer scope (CCR-NNN)` section verbatim in the prompt. Developer writes code + tests, runs self-checks, returns a structured implementation summary and `READY FOR REVIEW: CCR-NNN`.
8. **Dispatch `qa` and `reviewer` in parallel.** One message, two Agent tool blocks. To `qa`: pass team-lead's `## QA test plan (CCR-NNN)` section + a one-paragraph quote of the developer's "Files created or modified" list so qa knows what changed. To `reviewer`: pass team-lead's `## Reviewer focus (CCR-NNN)` section + the same files-changed paragraph.
9. **Dispatch `team-lead` (Mode 2).** Pass the developer's full implementation summary, qa's full response, and reviewer's full response. Team-lead either:
   - Returns `APPROVED: CCR-NNN` (or `FEATURE COMPLETE: <feature>` for the feature's last ticket) — proceed to step 10.
   - Returns `DISPATCH: <agent> CCR-NNN` with a `## Fix scope (CCR-NNN)` section in the body — go back to step 7 with a **fresh developer dispatch**, passing the fix scope verbatim. The dev session is new every time; the fix scope must be self-contained.
10. **On APPROVED — create the PR.** Per `WORKFLOW.md §Integration`:
    - Stage the ticket's files plus the team-lead's `TICKETS.md` / `CONTEXT.md` / `BRIEF.md` updates.
    - Commit with the format from WORKFLOW.md.
    - `git push -u origin ccr-NNN-<slug>`.
    - `gh pr create --base main --title "CCR-NNN: <title>" --body "<filled template>"`.
    - Report the PR URL to the user.
    - **Stop.** Never `gh pr merge`, never push to `main`, never force-push. The user merges manually.
11. **For new tickets** (user describes a phase or feature with no tickets yet): dispatch `project-manager` to slice it into `TICKETS.md` entries. PM does not need a branch — it edits `TICKETS.md` and the `.claude/docs/<feature>/` stubs on `main`.

### Boundary rules

- A ticket spanning `src/ccr/{bot|auth|...}` and `src/ccr/web/` must be split. If team-lead returns `BLOCKED: CCR-NNN — ticket spans web + python scope`, send it back to `project-manager` for two tickets.
- Multiple `[todo]` tickets can be dispatched in parallel only if **all** hold: different developer agents, no shared files, no `Depends on:` between them. Otherwise serialize. Parallel dispatches go in a single message with multiple Agent tool blocks. Note that the per-ticket flow (TL → dev → QA+review → TL) is sequential within a ticket; parallelism is between independent tickets.
- `qa` and `reviewer` always run in parallel within a single ticket — they have no dependency on each other.

## Project: Claude Code Remote

Self-hosted Telegram bot (one instance per project) that turns a phone into a remote control for a local Claude Code CLI session. Owner-controlled pairing via a console app on the project host; the bot forwards prompts to a managed Claude Code subprocess, streams structured events back to chat, handles permission prompts via inline buttons, and exposes a read-only multi-tab web viewer plus a localhost-app preview proxy through a user-supplied tunnel (Tailscale Funnel by default).

## Architecture (load-bearing decisions)

- **Modular monolith on one asyncio loop.** A single Python process runs aiogram (Telegram bot) + FastAPI (web/SSE/proxy) + the Claude Code subprocess manager, connected by an in-process pub/sub `EventBus`. The console (`python -m ccr console`) is a *separate short-lived process* that talks to the same SQLite DB — do not fold it into the server process.
- **State split: SQLite for metadata, JSONL for events.** Persistent SQL tables are `paired_users`, `pairing_codes`, `sessions` (see plan §5). Claude session events are *not* in SQL — they live as `data/logs/<session_id>.jsonl`, append-only, with sequence numbers derived from line count. SSE replay uses `?from=<seq>` then tails via the `EventBus`.
- **One running Claude session at a time, globally.** `SessionManager` enforces this; `started_by_tg_user_id` records who kicked it off but any paired user can interact and any paired user receives broadcasts.
- **Owner model.** First pairing approval auto-promotes to owner (partial unique index on `is_owner = true`). Subsequent approvals require an existing owner. Owner cannot be revoked. Allowlist is keyed on immutable `tg_user_id`, not `@username`.
- **Two JWT kinds, stateless, 30-min TTL.** `viewer` and `preview`. Tokens arrive as `?token=...` on `/auth`, get exchanged for an HttpOnly cookie. Preview tokens carry `payload.port` which must match the URL port — enforced in the proxy.
- **Discriminated-union event schema.** `ClaudeEvent` is a Pydantic discriminated union on `type`; unknown variants fall through to `UnknownEvent` rather than raising. This is the drift-tolerance strategy for Claude Code stream-json schema changes.
- **Permission gating.** While a `permission_request` is pending, the Telegram broadcast task pauses for that session and buffers events; SSE keeps streaming live. Resumes when any paired user taps a button.
- **CDN xterm.js with SRI.** The viewer loads xterm.js from jsDelivr with subresource-integrity hashes pinned in HTML. Trade-off: viewer needs internet. Vendoring is a one-line change if requirements shift.

## Tech stack

Python 3.12+ · `uv` (package manager + lockfile) · aiogram 3.x · FastAPI · uvicorn · httpx · Pydantic v2 + pydantic-settings · SQLAlchemy 2.x async + aiosqlite · Alembic · PyJWT · prompt_toolkit · structlog · pytest + pytest-asyncio · ruff (lint+format) · mypy --strict on `src/ccr`.

## Planned commands

These don't work yet. They are what the plan commits to building:

```bash
./install.sh                                  # one-shot bootstrap (Phase 14)
python -m ccr serve                           # bot + web server
python -m ccr console                         # interactive REPL for owner ops
python -m ccr pair {list|pending|approve|revoke|invite}   # one-shot equivalents
python -m ccr doctor                          # preflight checks
python -m ccr init-db                         # alembic upgrade head + ensure data dirs

uv sync                                       # install deps
ruff check src tests
ruff format --check
mypy src
pytest                                        # full suite
pytest tests/test_pairing.py                  # single file
pytest tests/test_pairing.py::test_name       # single test
pytest --cov=ccr --cov-fail-under=80          # what CI runs
```

CI runs ruff (check + format-check), mypy, and pytest with 80% coverage gate.

## Conventions baked into the plan

- **Layout.** Source under `src/ccr/` with subpackages `auth/ claude/ events/ bot/ web/ console/ db/`. Tests under `tests/` mirror module names. `data/` is gitignored.
- **Entrypoint.** `python -m ccr` → `ccr.cli:main` argparse dispatch. Also exposed as `ccr` console script via `[project.scripts]`.
- **Templates submodule.** `templates/` is a git submodule pointing at `dev-stack-agents`. Update with `git submodule update --remote templates`.
- **Tests use a fake Claude.** `tests/fakes/fake_claude.py` is a Python script that reads JSONL from stdin and emits canned JSONL — used in place of the real `claude` binary in `SessionManager` tests.
- **Settings.** Single Pydantic `Settings` loaded from `.env`; `JWT_SECRET` must be ≥ 32 chars; `PROXY_PORT_ALLOWLIST` blank means any port. See plan §7 for the full env list.
- **Phase order matters.** Phase 7 (Claude wrapper) must land before Phase 6 (session handlers), even though the plan lists them in the user-facing order. The plan flags this explicitly.
