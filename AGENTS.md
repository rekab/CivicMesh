# Repository Guidelines

## Project Structure & Module Organization
- Python modules live at the repo root (e.g., `web_server.py`, `mesh_bot.py`, `database.py`, `config.py`).
- Static web assets are in `static/` (`index.html`, `app.js`, `style.css`).
- Runtime configuration is in `config.toml`; logs default to `logs/`.
- The SQLite database defaults to `civic_mesh.db` (configurable via `config.toml`).

## Build, Test, and Development Commands
- Create and activate a local virtual environment (unprivileged):
  - `python3 -m venv .venv`
  - `source .venv/bin/activate`
  - `pip install -U pip`
  - `pip install .`
- Run the captive portal server:
  - `python3 web_server.py --config config.toml`
  - or after install: `civicmesh-web --config config.toml`
- Run the MeshCore relay bot:
  - `python3 mesh_bot.py --config config.toml`
  - or after install: `civicmesh-mesh --config config.toml`
- Admin CLI (SSH only):
  - `civicmesh-admin --config config.toml stats`

## Coding Style & Naming Conventions
- Use 4-space indentation and Python 3.9+ syntax.
- Prefer explicit, descriptive names (`mesh_bot`, `web_server`, `outbox_batch_size`).
- Keep modules small and focused; this repo uses single-file modules at the root.
- There is no formatter or linter configured yet; follow existing file style.

## Testing Guidelines
- No automated test suite is present in the repository.
- If you add tests, introduce a `tests/` directory and document how to run them.
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
