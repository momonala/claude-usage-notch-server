# claude-usage-notch-server

[![CI](https://github.com/momonala/claude-usage-notch-server/actions/workflows/ci.yml/badge.svg)](https://github.com/momonala/claude-usage-notch-server/actions/workflows/ci.yml)

A dumb store for Claude Code usage records. The [ClaudeUsageNotch](https://github.com/momonala/ClaudeUsageNotch)
macOS app parses `~/.claude/projects/**/*.jsonl`, extracts one record per `assistant`
turn, and POSTs them here. The app's analytics chart reads them back. This lets usage
history outlive the ~30-day JSONL retention and loads the chart faster than re-parsing
local files.

The server does no aggregation — it stores raw token counts and serves them back. All
compute happens in Swift.

## Prerequisites

- Python 3.12+
- [uv](https://github.com/astral-sh/uv) for dependency management

## Configuration

There are no secrets, so there is no `.env`. All config lives in `pyproject.toml`
under `[tool.config]` and is read by `src/config.py`.

```toml
[tool.config]
flask_port = 5014          # port the API listens on
flask_host = "0.0.0.0"     # bind address
db_path = "claude-usage.db"  # SQLite file, relative to the working directory
```

`install.sh` reads `flask_port` and the project name via `uv run config` to wire up
the systemd service and Cloudflare route.

## Running

```bash
uv sync
uv run app          # Flask API on flask_port
uv run scheduler    # hourly git backup of the DB (separate process)
```

## Architecture

```
ClaudeUsageNotch app ──POST /api/records──► Flask API ──► SQLite (usage_records)
        ▲                                       │
        └──────────GET /api/records?since=──────┘

scheduler (separate process) ──hourly──► git commit of <DB_PATH>.bk
```

The schema is a single table created on startup via SQLAlchemy `create_all` — no
migration tooling, since it's a single-user store with no reverse-compatibility needs.

| File | Role |
|------|------|
| `src/app.py` | Flask app factory + entry point (`uv run app`) |
| `src/routes.py` | The three endpoints below |
| `src/models.py` | `UsageRecord` model + JSON (de)serialization |
| `src/database.py` | Engine, `session_scope`, `init_db` |
| `src/config.py` | All config (read from `pyproject.toml`) + `DATABASE_URL`; CLI for install scripts |
| `src/scheduler.py` | Periodic background tasks (currently hourly DB git backup) |
| `src/git_tool.py` | Commits `<DB_PATH>.bk` when the DB changes |

## API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/status` | GET | Health check → `{"status": "ok"}` |
| `/api/records` | POST | Upsert a batch (idempotent by `uuid`) |
| `/api/records` | GET | Records with `timestamp >= since` |

### POST /api/records

Body is a JSON **array** of records. Upsert is `INSERT … ON CONFLICT(uuid) DO NOTHING`,
so re-posting a batch is safe.

```bash
curl -X POST http://localhost:5014/api/records \
  -H 'Content-Type: application/json' \
  -d '[{"uuid":"b477...","session_id":"d8e2...","timestamp":"2026-06-09T12:34:54.292Z","cwd":"/Users/me/code/Proj","project":"Proj","model":"claude-sonnet-4-6","is_sidechain":false,"input_tokens":2,"output_tokens":183,"cache_creation_tokens":7684,"cache_read_tokens":12081,"ephemeral_1h_tokens":7684,"ephemeral_5m_tokens":0,"web_searches":0,"web_fetches":0}]'
```

Response: `{"inserted": N, "skipped": M}` (skipped = already-present uuids).

### GET /api/records

```bash
curl 'http://localhost:5014/api/records?since=2026-06-01T00:00:00Z'
```

Returns a JSON array ordered by `timestamp`. Omit `since` for everything.

### Record schema

`uuid` (primary key) and `timestamp` are required; everything else is nullable or
defaults to 0. Timestamps are ISO8601, serialized back with a trailing `Z`.

```
uuid, request_id, session_id, parent_uuid, timestamp, cwd, project, git_branch,
model, version, entrypoint, attribution_skill, is_sidechain, stop_reason,
service_tier, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
ephemeral_1h_tokens, ephemeral_5m_tokens, web_searches, web_fetches
```

`ingested_at` is server-owned and not part of the API.

## Deployment

`install/install.sh` installs two systemd services on the Raspberry Pi — the Flask API
(`projects_claude-usage-notch-server.service`) and the scheduler
(`projects_claude-usage-notch-server_scheduler.service`) — and registers a Cloudflare
tunnel route at `claude-usage-notch-server.mnalavadi.org`.
