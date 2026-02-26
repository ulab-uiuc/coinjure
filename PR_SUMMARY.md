# PR Summary: Split Working Tree into Reviewable PRs

## Branch Overview

| Branch           | Base             | Target         | Description                     |
| ---------------- | ---------------- | -------------- | ------------------------------- |
| `pr1-packaging`  | `e92c6a0` (main) | main           | Packaging & release prep        |
| `pr2-interfaces` | `e92c6a0` (main) | main           | Small interface additions       |
| `pr3-engine`     | `pr2-interfaces` | pr2-interfaces | TradingEngine upgrade (stacked) |
| `pr4-monitor`    | `pr3-engine`     | pr3-engine     | Monitor TUI dashboard (stacked) |

**Merge order:** PR-1 → PR-2 → PR-3 → PR-4

---

## PR-1: Packaging & Release Prep

**Branch:** `pr1-packaging`

### Files Changed

- `pyproject.toml` — Relax Python constraint `>=3.10, <3.12` → `>=3.10, <4.0`; fix dependencies
- `README.md` — Shorten for PyPI; add monitor CLI examples; update env vars section
- `docs/PROJECT_SPECIFICATION.md` — New project specification doc (Chinese)

### Why

- Enable Python 3.12+ support
- Improve PyPI readiness and documentation
- Add internal project spec for onboarding

### How to Verify

```bash
pip install -e .
pm-cli --help
pm-cli monitor --help
```

### Risks / Breaking Changes

- **None.** Backward-compatible.
- Note: `docs/PROJECT_SPECIFICATION.md` is in Chinese (internal doc).

### Secrets Scan

- README env var placeholders (`POLYMARKET_PRIVATE_KEY`, `NEWS_API_KEY`) — not real secrets. ✓

### Ping the Lead

> **PR-1: Packaging & release prep** > [PR link]
>
> Changes: pyproject.toml (Python >=3.10,<4.0), README shortened for PyPI, new docs/PROJECT_SPECIFICATION.md.
>
> Verify:
>
> ```bash
> pip install -e .
> pm-cli --help
> pm-cli monitor --help
> ```

---

## PR-2: Small Interface Additions

**Branch:** `pr2-interfaces`

### Files Changed

- `pm_cli/data/data_source.py` — Add default `start()` / `stop()` lifecycle hooks
- `pm_cli/trader/trader.py` — Add `orders` attribute to base class

### Why

- DataSource needs lifecycle hooks for live sources (start/stop polling)
- Trader needs `orders` for monitor dashboards and order tracking

### How to Verify

```bash
pip install -e .
pm-cli --help
pm-cli monitor --help
```

### Risks / Breaking Changes

- **None.** Additive only. Default `start()`/`stop()` are no-ops; subclasses override.
- `Trader.orders` initialized to `[]`; existing subclasses inherit it.

### Secrets Scan

- No matches. ✓

### Ping the Lead

> **PR-2: Interface additions (DataSource, Trader)** > [PR link]
>
> Adds DataSource `start()`/`stop()` lifecycle hooks and `Trader.orders` attribute. Additive, no API changes.
>
> Verify:
>
> ```bash
> pip install -e .
> pm-cli --help
> ```

---

## PR-3: TradingEngine Upgrade

**Branch:** `pr3-engine` (base: `pr2-interfaces`)

**PR target:** `pr2-interfaces` (merge after PR-2)

### Files Changed

- `pm_cli/core/trading_engine.py` — EngineSnapshot, get_snapshot(), async stop(), exception backoff, request_stop()
- `pm_cli/data/live/live_data_source.py` — `_poll_task` handling, `stop()` for all three live sources

### Why

- Snapshot/lifecycle for monitor dashboards
- Graceful stop with data-source cleanup (no task leaks)
- Exception backoff and continuous None handling

### How to Verify

```bash
pip install -e .
pm-cli --help
pm-cli monitor --help
```

### Risks / Breaking Changes

- **Depends on PR-2.** Must merge PR-2 first.
- TradingEngine API: `start()` unchanged; adds `stop()`, `get_snapshot()`, `request_stop()` — additive.

### Secrets Scan

- No matches. ✓

### Ping the Lead

> **PR-3: TradingEngine upgrade** > [PR link]
>
> Adds EngineSnapshot, get_snapshot(), async stop() with data-source cleanup, exception backoff. Depends on PR-2.
>
> Verify:
>
> ```bash
> pip install -e .
> pm-cli --help
> pm-cli monitor --help
> ```

---

## PR-4: Monitor TUI Dashboard

**Branch:** `pr4-monitor` (base: `pr3-engine`)

**PR target:** `pr3-engine` (merge after PR-3)

### Files Changed

- `pm_cli/cli/monitor.py` — Full-screen Rich TUI, 8 panels, TUILogHandler, DemoDataSource/DemoStrategy, --demo/--live/--paper/--real-trades

### Why

- Rich full-screen monitoring UI
- Demo mode for testing without live API
- Backward-compatible with older `monitor` entrypoints

### How to Verify

```bash
pip install -e .   # Requires PR-1 for Python 3.12+; or use Python 3.10–3.11
pm-cli --help
pm-cli monitor --help
pm-cli monitor --demo   # Run 10–15 s, then Ctrl+C
```

After Ctrl+C: no traceback, no "Task was destroyed but it is pending!", terminal returns to normal.

### Risks / Breaking Changes

- **Depends on PR-3 (and thus PR-2).** Merge in order.
- **Backward compatibility:** Old `monitor` CLI (`-w`, `-r`, `-c`) still supported; new flags `--demo`, `--live`, `--paper`, `--real-trades` added.
- Note: `pm-cli monitor` without `--live` now defaults to `--demo` (simulated data) instead of requiring a config. If external scripts relied on the old "single snapshot" default, they should use `--no-watch` explicitly.

### Secrets Scan

- `POLYMARKET_PRIVATE_KEY` env var references — placeholder/help text only. ✓
- `simple_strategy.py` contains a hardcoded API key; **not modified** in this PR (per lead: no changes to external API logic).

### Ping the Lead

> **PR-4: Monitor TUI dashboard** > [PR link]
>
> Full-screen Rich TUI monitor with --demo, --live, --paper, --real-trades. Depends on PR-3.
>
> Verify:
>
> ```bash
> pip install -e .
> pm-cli monitor --help
> pm-cli monitor --demo   # Run 10–15 s, Ctrl+C — no traceback, no task leaks
> ```

---

## Secrets Note

- **Known risk:** `pm_cli/strategy/simple_strategy.py` line 89 contains a hardcoded API key. Per lead instruction, this file is **not modified** in any PR. No new secrets introduced.

---

## Push & Open PRs

```bash
git push origin pr1-packaging
git push origin pr2-interfaces
git push origin pr3-engine
git push origin pr4-monitor
```

Then open PRs on GitHub:

- PR-1: `pr1-packaging` → `main`
- PR-2: `pr2-interfaces` → `main`
- PR-3: `pr3-engine` → `pr2-interfaces`
- PR-4: `pr4-monitor` → `pr3-engine`
