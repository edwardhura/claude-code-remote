# Brief: core

## Purpose
The root scaffold every other feature builds on: Python package layout, `uv` dep set, CI, pre-commit, the `Settings` object, structured logging, the SQLAlchemy 2 ORM models, the async engine, and the initial Alembic migration. Establishes the conventions (argparse-based CLI, `python -m ccr` + `ccr` console script, `mypy --strict` on `src/ccr`, 80% coverage gate) every later feature respects.

## Key invariants
- Single-owner partial unique index on `paired_users(is_owner = true)` enforces that at most one row can have `is_owner = TRUE` — `IntegrityError` on a second insert.
- `JWT_SECRET` must be ≥ 32 characters; `WEB_PORT` ∈ 1..65535; `TOKEN_TTL_SECONDS` ∈ 60..86400. There is no default fallback for `JWT_SECRET` — Settings validation fails fast.
- `PROXY_PORT_ALLOWLIST` blank ⇒ `set[int] | None = None` (any port allowed); otherwise comma-separated ints.
- DB lives at `sqlite+aiosqlite:///data/ccr.db`; UUID columns use `Uuid(as_uuid=True)`.
- `init-db` runs `alembic upgrade head` programmatically and ensures `DATA_DIR` + `DATA_DIR/logs` exist.
- `mypy --strict` is scoped to `src/ccr/` only; CI's coverage gate is `--cov-fail-under=80`.

## Public surface
- `pyproject.toml` — project metadata, runtime + dev deps, ruff/mypy/pytest config, `[project.scripts] ccr = "ccr.cli:main"`.
- `.env.example` — env template matching plan §7 (TELEGRAM_BOT_TOKEN, PUBLIC_URL, JWT_SECRET, plus optional defaults).
- `.pre-commit-config.yaml` — ruff, ruff-format, mypy --strict hooks (mypy scoped to `src/ccr/`).
- `.github/workflows/ci.yml` — pinned `uv`, `uv sync --frozen`, ruff check + format check, mypy, pytest with `--cov-fail-under=80`, Python 3.12.
- `src/ccr/__init__.py` — exposes `__version__ = "0.1.0"`.
- `src/ccr/__main__.py` — `python -m ccr` entry calling `ccr.cli:main`.
- `src/ccr/cli.py` — argparse skeleton; `init-db` handler runs `alembic upgrade head`; other handlers raise `NotImplementedError` until later phases land. `build_parser()` exposed for tests.
- `src/ccr/config.py::Settings(BaseSettings)` — every Section 7 env var; field validators for the constraints above.
- `src/ccr/logging_setup.py::configure_logging(level)` — wires structlog + stdlib to a console renderer; idempotent.
- `src/ccr/db/__init__.py` — re-exports `Base`, `PairedUser`, `PairingCode`, `Session`.
- `src/ccr/db/models.py` — SQLAlchemy 2 `DeclarativeBase` + the three persistent tables.
- `src/ccr/db/engine.py::create_engine_from_settings`, `AsyncSessionMaker`, `get_session` — async context manager committing on success / rolling back on error.
- `alembic.ini` + `alembic/env.py` (async-aware) + `alembic/versions/0001_initial.py` — paired_users, pairing_codes, sessions tables and the index set below.

## Subtleties / gotchas
- `alembic/env.py` is async-aware — runs sync migration functions inside an async wrapper. Future migrations follow the same template.
- Index list to remember: single-owner partial unique on `paired_users`, `pairing_codes(tg_user_id, used_at)`, `sessions(status)`, `sessions(started_at desc)`.
- The CLI dispatcher is the single argparse entry point — every future subcommand (`pair`, `console`, `serve`, `doctor`, `init-db`) plugs in here. Don't introduce a second CLI library.

## Cross-feature relations
- depends on: (none — this is the root scaffold)
- used by: every other feature (auth, console, chat-bot, claude-runtime, web-viewer, bootstrap) inherits the package layout, dep set, CLI dispatch, Settings object, and DB schema defined here.

## Status
- State: COMPLETE
- Tickets: CCR-001, CCR-002, CCR-003
- Last updated: CCR-003 (2026-04-26)
