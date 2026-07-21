# Repository Guidelines

## Project Structure & Module Organization
- `main.py` is the entry point and calls into the `monitor` package.
- `monitor/` contains the core modules (app loop, fetcher, targets, history, notifications, utilities).
- Root JSON/log files are runtime state: `cookie.txt`, `monitor_settings.json`, `rssi_threshold.json`, `zte_clients_state.json`, `target_history.json`, `target_events.log`, `target_status.log`. Treat them as local data, not source.

## Build, Test, and Development Commands
- `python main.py` runs the monitor loop using settings in `monitor/config.py`.
- `main.bat` runs the same entry point on Windows.
- `pip install requests` installs the only external dependency.

## Coding Style & Naming Conventions
- Python 3.10+; 4-space indentation; follow PEP 8.
- Use `snake_case` for functions/variables, `PascalCase` for classes, and `UPPER_SNAKE_CASE` for module constants (see `monitor/config.py`).
- Keep configuration in `monitor/config.py` and JSON files in the repo root; avoid hardcoding new secrets in code.

## Testing Guidelines
- No automated test suite is configured in this repo.
- If adding tests, place them under `tests/` and use `test_*.py`; run with `python -m pytest`.

## Commit & Pull Request Guidelines
- No Git history is present in this workspace; use concise, imperative commit messages (e.g., "Add ping watchdog").
- PRs should include a short summary, list config/data changes, and note any new settings or external dependencies.

## Configuration & Security Notes
- Router credentials, webhook URLs, and PushPlus tokens live in `monitor/config.py`; keep local values private and rotate before sharing.
- Runtime files like `target_history.json` can be large; avoid committing generated data.
