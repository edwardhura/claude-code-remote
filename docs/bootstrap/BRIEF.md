# Brief: bootstrap

## Purpose
First-run install + preflight: `install.sh` (one-shot bootstrap — Python + `uv`, virtualenv, `.env` seeding, DB init) and the `doctor` subcommand (preflight checks for required env vars, DB reachability, optional CDN + `PUBLIC_URL` checks). Both are owned by python-developer per `WORKFLOW.md`. Not yet implemented.

## Key invariants
_(team lead appends from each developer's BRIEF update note as tickets land)_

Anticipated (from plan §14):
- `install.sh` uses `set -euo pipefail` and is **idempotent** — re-running is a no-op except for re-printing next steps.
- `JWT_SECRET` is generated **only if** `.env` doesn't already define one — never overwrite an existing secret.
- `doctor` exits 0 when all required checks pass, exits 1 on any required-check failure. Optional checks (CDN reachability, `PUBLIC_URL` resolution) print warnings without affecting exit code.

## Public surface
_(team lead appends from each developer's BRIEF update note as tickets land)_

Anticipated:
- `./install.sh` (repo-root) — one-shot bootstrap.
- `python -m ccr doctor` / `ccr doctor` — preflight subcommand.
- `src/ccr/doctor.py` (or in-line in `cli.py`) — check helpers.

## Subtleties / gotchas
_(team lead appends from each developer's BRIEF update note as tickets land)_

## Cross-feature relations
- depends on: core (Settings + CLI dispatch + DB engine), auth (planned — `doctor` may verify owner-presence as a soft check).
- used by: nothing (top of the dependency stack; runs once at install time).

## Status
- State: IN PROGRESS
- Tickets: CCR-016, CCR-017
- Last updated: _(none yet — first APPROVED bumps this)_
