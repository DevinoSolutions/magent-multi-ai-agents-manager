# Contributing to magent

Thanks for your interest in improving magent! This guide covers local setup,
the quality gate every change must pass, and the conventions the codebase
enforces. Please open an issue to discuss substantial changes before starting,
so we can agree on the approach.

## Development setup

```bash
pip install -e ".[dev]"     # or: uv sync --extra dev   (uv.lock is committed)
npm install                 # one-time: activates the husky git hooks
```

Optional extras:

- `pip install -e ".[sentry]"` — env-gated Sentry error reporting (`MAGENT_SENTRY_DSN`).
- `pip install -e ".[toast]"` — Windows desktop toast for `magent attention`.
- `pip install -e ".[qr]"` — QR code for `magent mobile`.

## The quality gate

**`scripts/check.py` must exit 0 before every commit.** The husky pre-commit hook
runs the fast gate automatically; the pre-push hook runs the full gate as a
second line of defense.

```bash
uv run python scripts/check.py          # full gate (default)
uv run python scripts/check.py --fast   # pre-commit: seconds, not minutes
```

The **full** gate runs, in order:

- `ruff check` — lint (with the Python 3.10 syntax floor)
- `ruff format --check` — formatting
- custom lint — `scripts/lint_rules.py`, rules MD001–MD005
- `ty` strict type check — src + scripts, plus a second win32-platform pass over
  `platform/windows.py` + `hotkey.py`
- `compileall`
- `vulture` — dead-code detection
- `pytest tests/unit/` with coverage (`--cov-fail-under=52`)

The **fast** gate runs ruff + format + custom lint + both ty passes only.

## Running tests

```bash
pytest tests/unit/ -q                        # fast unit suite, safe anywhere
pytest tests/unit/test_x.py::test_y          # a single test
```

> **Heads-up:** a bare `pytest` also collects the `platform/`, `e2e`, and `dist`
> tiers — real monitors/terminals, a live SSH server, and a wheel build. CI splits
> those into separate jobs. Don't run a bare `pytest` locally and read a clean exit
> as "the gate passed"; run `tests/unit/` (or `scripts/check.py`) instead.

## Commit & PR conventions

- **Conventional Commits**: `feat(scope): …`, `fix`, `refactor`, `test`, `chore`,
  `ci`, `docs`. Check `git log --oneline` for the pattern in force.
- **PRs target `main` only.** `main` is protected and must be up to date before
  merge.
- Keep the gate green, update docs when behavior changes, and add or update tests
  for the code you touch.

## Code conventions

These are enforced by the gate (ruff TID251 bans + the custom lint) — a violation
reddens CI:

- **No `typing.Any`** in `src/`. Use `object` + `isinstance` narrowing.
- **Env access only via `magent.env`** — `os.environ` / `os.getenv` are banned
  elsewhere.
- **Styling via `magent.style`** — import `style`, never `click.style` directly.
- **Platform capabilities are gated on `supports_*()` probes**, not bare
  `sys.platform` checks in business logic.
- **Adding a deeply-integrated agent tool** = one new `sessions/<tool>.py` + one
  `AGENT_TOOLS` entry, with no dispatcher change. A plain command tool (no session
  resume) is just a `config.DEFAULT_TOOLS` entry.

## Architecture & known debt

`CLAUDE.md` is the terse map for a cold start. **`DESIGN.md`** holds the
architectural rationale and the **known-debt ledger** (§3). Please don't
drive-by-fix ledger items — they are tracked, deliberate debt; raise them
separately.
