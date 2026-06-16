# claude-usage-notch-server

[![CI](https://github.com/momonala/claude-usage-notch-server/actions/workflows/ci.yml/badge.svg)](https://github.com/momonala/claude-usage-notch-server/actions/workflows/ci.yml)

A store for Claude Code usage records. The [ClaudeUsageNotch](https://github.com/momonala/ClaudeUsageNotch)
macOS app parses `~/.claude/projects/**/*.jsonl`, extracts one record per `assistant`
turn, and POSTs them here. The app's analytics chart reads them back. This lets usage
history outlive the ~30-day JSONL retention and loads the chart faster than re-parsing
local files.

It stores raw token counts and serves them back via `/api/records`, and also aggregates
them on demand for the chart via `/api/analytics` (cost, cache-hit rate, model/project/
skill breakdowns, daily series, time buckets). The aggregation lives in `src/analytics.py`
and previously ran in Swift.

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
```

## Architecture

```
ClaudeUsageNotch app ──POST /api/records──► Flask API ──► SQLite (usage_records)
        ▲                                       │
        ├──────────GET /api/records?since=──────┤
        └──────────GET /api/analytics──────────►┘  (aggregated in src/analytics.py)
```

The schema is a single table created on startup via SQLAlchemy `create_all` — no
migration tooling, since it's a single-user store with no reverse-compatibility needs.

| File | Role |
|------|------|
| `src/app.py` | Flask app factory + entry point (`uv run app`) |
| `src/routes.py` | The four endpoints below |
| `src/models.py` | `UsageRecord` model + JSON (de)serialization |
| `src/analytics.py` | On-demand aggregation for `/api/analytics` (cost, breakdowns, buckets) |
| `src/database.py` | Engine, `session_scope`, `init_db` |
| `src/config.py` | All config (read from `pyproject.toml`) + `DATABASE_URL`; CLI for install scripts |

## API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/status` | GET | Health check → `{"status": "ok"}` |
| `/api/records` | POST | Upsert a batch (idempotent by `uuid`) |
| `/api/records` | GET | Records with `timestamp >= since` |
| `/api/quota_snapshots` | POST | Upsert a batch of polled quota readings (idempotent by `window_type`+`timestamp`) |
| `/api/quota_snapshots` | GET | Readings with `timestamp >= since`, optionally filtered by `window_type` |
| `/api/analytics` | GET | Pre-aggregated chart data for the session/weekly/month + lookback windows |

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

### POST /api/quota_snapshots

Body is a JSON **array** of polled quota readings — one per window the client just
fetched from the provider (e.g. Claude's `five_hour` and `seven_day`). Upsert is
`INSERT … ON CONFLICT(window_type, timestamp) DO NOTHING`, so re-posting (or two
devices polling the same account a moment apart) is safe.

```bash
curl -X POST http://localhost:5014/api/quota_snapshots \
  -H 'Content-Type: application/json' \
  -d '[{"window_type":"session","timestamp":"2026-06-16T12:34:00.000Z","percent_used":0.42,"resets_at":"2026-06-16T17:00:00.000Z","source":"MacBook-Pro"}]'
```

Response: `{"inserted": N, "skipped": M}`.

### GET /api/quota_snapshots

```bash
curl 'http://localhost:5014/api/quota_snapshots?window_type=session&since=2026-06-01T00:00:00Z'
```

Returns a JSON array ordered by `timestamp`. `window_type` and `since` are both optional.

### GET /api/analytics

Aggregates records into the chart payload the app renders. The four `*_since` params are
required ISO8601 timestamps marking the start of each window; `granularity` is optional:

```bash
curl 'http://localhost:5014/api/analytics?session_since=2026-06-12T07:00:00Z&weekly_since=2026-06-06T00:00:00Z&month_since=2026-05-13T00:00:00Z&lookback_since=2026-05-13T00:00:00Z&granularity=day'
```

`session_since` / `weekly_since` / `month_since` are fixed reference windows (5h / 7d /
30d) driving the cost pills and the session/weekly charts. `lookback_since` follows the
app's 1D/7D/30D/All selector and drives the period-labeled breakdowns and spend/sessions
series — keep it independent of the fixed windows so, e.g., switching to 7D doesn't shrink
the "Month" figure.

`granularity` sets the spend/sessions bucket width: `hour` (the 1D view → 24 hourly
buckets), `day` (7D/30D → daily buckets, the default), or `month` (All → one bucket per
calendar month from the first record). Bucket starts in `daily_cost` / `daily_sessions`
are ISO8601 timestamps regardless of granularity.

Returns costs (`session_cost`, `weekly_cost`, `month_cost`, `lifetime_cost`, …),
`cache_hit_rate`, token-type fractions, `model_breakdown` / `project_breakdown` /
`skill_breakdown`, `daily_cost` / `daily_sessions`, per-minute `session_buckets` +
per-hour `weekly_buckets` (token counts), and `session_quota_history` /
`weekly_quota_history` — the real polled `quota_snapshots` readings covering the same
spans as `session_buckets` / `weekly_buckets`, oldest first. The latter two are empty
until a client has pushed at least one reading for that window; the app falls back to
a token-based estimate when empty (see `UsageChartView.swift`). The DB fetch floor is
`min(lookback_since, month_since)`; only `lifetime_cost` scans the full history (cost
columns only). See `src/analytics.py`.

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

### Quota snapshot schema

`window_type` + `timestamp` form the composite primary key. `percent_used` is
required (0...1+); `resets_at` and `source` (the polling device's hostname) are
optional. `ingested_at` is server-owned.

```
window_type, timestamp, percent_used, resets_at, source
```

## Deployment

`install/install.sh` installs a systemd service on the Raspberry Pi
(`projects_claude-usage-notch-server.service`) and registers a Cloudflare
tunnel route at `claude-usage-notch-server.mnalavadi.org`.
