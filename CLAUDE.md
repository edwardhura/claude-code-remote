# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository state

This repo is **pre-implementation**. The only tracked source-of-truth file is `claude-code-remote-plan.md` — a 1200-line architecture & phased implementation plan. There is no `src/`, `pyproject.toml`, tests, or `.env.example` yet. Treat the plan as the authoritative spec; when starting work, read the relevant phase section first and follow the file paths, schemas, and acceptance criteria it prescribes.

When the user asks to "implement Phase N" or to add a feature, the plan's Section 8 (Implementation Phases) defines the file list, packages, tasks, and acceptance criteria for that phase. Do not invent a different layout — the phases are designed to build on each other.

## Multi-agent workflow

Implementation is driven by specialist subagents in `.claude/agents/` (`project-manager`, `team-lead`, `python-developer`, `web-developer`, `qa`, `reviewer`). The shared protocol — ticket schema, status transitions, BRIEF/CONTEXT format, verdict strings, per-ticket flow — lives in `docs/WORKFLOW.md`. Ticket state is split across two files at the repo root:

- `BACKLOG.md` — active tickets in statuses `todo` / `in-progress` / `blocked` (status in the title for grep-ability).
- `DONE.md` — archived tickets in statuses `done` / `closed`. Entries move here from `BACKLOG.md` when the team-lead approves a ticket (`done`) or the main session closes one (`closed`, e.g. abandoned/auto-rejected).

Per-feature design notes live under `docs/<feature>/{BRIEF,CONTEXT}.md`. `BRIEF.md` is the dense, always-current summary the team-lead works from (purpose, invariants, public surface, status). `CONTEXT.md` is the deeper file/relations/change-history record kept by the team-lead from developer reports. Architect plan files live at `plans/CCR-NNN-<slug>.md`. Both ticket files are append-only — never delete content; only flip the status in the title or move the entry from `BACKLOG.md` to `DONE.md`.

The orchestrator role — picking tickets, dispatching agents, committing, opening PRs — is **not** part of the default session. It is loaded on demand via the `/implement-ticket` skill, which the user invokes when they want ticket work driven end-to-end. Without that skill, treat agent invocation as opt-in: do not preemptively dispatch subagents, do not assume the session is running the multi-agent loop. Direct questions, ad-hoc edits, and debugging help are answered directly.

A future `/research` skill will cover the project-manager-driven ticket-slicing flow.

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
