# GEMINI.md — User Preferences

**Project:** mc-clanker — AI-powered continuous music generator

## Workflow

- Use brainstorming before implementing new features or significant refactors
- Use TDD for bug fixes and small features; verify before claiming work complete
- Always check for relevant skills before responding or taking action

## Code Style

- Follow existing patterns in the codebase (module imports, state access, async patterns)
- Never call framework functions directly from API handlers — update state only
- Always use `with state.lock:` for shared state access
- Use `datetime.now(timezone.utc)` — never `datetime.utcnow()`
- Use `asyncio.get_running_loop()` in async functions — never `get_event_loop()`
- Never use bare `except:` — always `except Exception:` minimum
- All configuration files are in `config/` directory

## Architecture Notes

- The async framework loop (`framework_main_async.py`) is the active implementation — do not modify the old sync framework
- The worker runs in a separate container and communicates via PostgreSQL job queue
- Audio storage uses Garage/MinIO S3-compatible object storage via `garage_client.py`
- All API routes are in `app/routes/` — route modules aggregated in `app/routes/__init__.py`
