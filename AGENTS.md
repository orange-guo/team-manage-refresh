# AGENTS.md

## Commands
- Install runtime deps with `pip install -r requirements.txt`; there is no `pyproject.toml`, Makefile, CI workflow, or configured linter/typechecker in this repo.
- Run the app locally with `python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8008` or `python -m app.main`.
- Run the focused unit suite with `python -m unittest discover -s tests`; these tests use in-memory SQLite and mock ChatGPT calls.
- Do not use root-level unittest discovery: `test_webhook.py` is a manual integration script, imports `pytest`, and mutates `./team_manage.db` if run directly.
- Docker uses `docker compose up -d --build`; the host port is `61210` (`61210:8008`), not the app's internal `8008`.

## Runtime And Data
- `app/main.py` is the FastAPI entrypoint; startup creates tables, runs `app/db_migrations.py`, initializes the admin password if missing, and starts APScheduler jobs.
- `init_db.py` is only for pre-seeding default `Setting` rows/admin hash; startup already handles table creation and migrations.
- DB location depends on env: code default is `data/team_manage.db`, `.env.example` uses `./team_manage.db`, and Docker forces `/app/data/team_manage.db` via compose.
- `SECRET_KEY` derives the Fernet key for stored AT/RT/ST/ID tokens; changing it on an existing DB breaks token decryption.

## Architecture Notes
- Routes in `app/routes/` should stay thin; business rules live in `app/services/`, SQLAlchemy models in `app/models.py`, templates in `app/templates/`, and static assets in `app/static/`.
- There is no Alembic: when schema changes, update both `app/models.py` and the SQLite auto-migration logic in `app/db_migrations.py`.
- Normal and welfare inventory are separated by `pool_type` on `Team` and `RedemptionCode`; preserve pool filters when changing redemption, admin, or stock logic.
- The `/free` experience flow uses `app/services/experience.py` plus `experience_assignments` and `experience_queue`, separate from redemption codes.
- Redemption concurrency relies on `app/services/redeem_flow.py` locks plus `TeamService.reserve_seat_if_available()` atomic updates; avoid bypassing those service paths for seat assignment.
- Settings are cached in-process by `settings_service`; tests or scripts that write `Setting` rows directly may need `settings_service.clear_cache()`.

## Integrations
- ChatGPT calls use `curl_cffi` with `impersonate="chrome110"`; proxy settings come from DB settings, not only environment variables.
- Admin automation can authenticate protected admin endpoints with `X-API-Key`; the key is stored in the `api_key` setting and documented in `integration_docs.md`.
