# Repository Guidelines

## Project Structure & Module Organization
- Python modules live at the repo root (e.g., `web_server.py`, `mesh_bot.py`, `database.py`, `config.py`).
- Static web assets are in `static/` (`index.html`, `app.js`, `style.css`).
- Runtime configuration is in `config.toml`; logs default to `logs/`.
- The SQLite database defaults to `civic_mesh.db` (configurable via `config.toml`).

## Build, Test, and Development Commands
- Sync dependencies into a project-local venv (managed by uv):
  - `uv sync`
- Run the captive portal server:
  - `uv run civicmesh-web --config config.toml`
- Run the MeshCore relay bot:
  - `uv run civicmesh-mesh --config config.toml`
- Admin CLI (SSH only):
  - `uv run civicmesh --config config.toml stats`

## Coding Style & Naming Conventions
- Use 4-space indentation and Python 3.13+ syntax.
- Prefer explicit, descriptive names (`mesh_bot`, `web_server`, `outbox_batch_size`).
- Keep modules small and focused; this repo uses single-file modules at the root.
- There is no formatter or linter configured yet; follow existing file style.

## Testing Guidelines
- Tests live in `tests/` and use the `unittest` framework (stdlib, no extra dependency).
- Run all tests: `uv run python -m unittest`
- Run a single test module: `uv run python -m unittest tests.test_db_lock_retry -v`
- When making changes, perform manual smoke checks (start both processes and verify UI flows).

## Commit & Pull Request Guidelines
- Git history currently contains only an initial commit; no formal convention is established.
- Use short, imperative commit summaries (e.g., "Add outbox retry logging").
- PRs should include a brief summary, configuration changes, and manual test notes.
- Include UI screenshots or short clips if `static/` is modified.

## Security & Configuration Notes
- The app runs an HTTP-only captive portal; avoid adding features that imply transport security.
- Update `config.toml` when changing defaults (serial port, SSID, channels, limits).
- Security events are logged to `logs/security.log`; keep new logging consistent with existing patterns.
