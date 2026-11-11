# claude-usage-notch-server

[![CI](https://github.com/momonala/claude-usage-notch-server/actions/workflows/ci.yml/badge.svg)](https://github.com/momonala/claude-usage-notch-server/actions/workflows/ci.yml)

A store for Claude Code usage records. The [ClaudeUsageNotch](https://github.com/momonala/ClaudeUsageNotch)
macOS app parses `~/.claude/projects/**/*.jsonl`, extracts one record per `assistant`
line, and POSTs them here. Claude Code writes one line per content block of a response,
each repeating that request's token usage, so the server stores one row per `request_id`. The app's analytics chart reads them back. This lets usage
history outlive the ~30-day JSONL retention and loads the chart faster than re-parsing
local files.

It aggregates them on demand for the chart via `/api/analytics` (cost, cache-hit rate, model/project/
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
db_path = "data/claude-usage.db"  # SQLite file, relative to the working directory
spyglass_host = "localhost:5013"  # where this service's own metrics/logs are shipped
spyglass_dashboard_url = "https://spyglass.mnalavadi.org/dashboard/claude-usage-notch-server"
```

`install.sh` reads `flask_port` and the project name via `uv run config` to wire up
the systemd service and Cloudflare route.

## Running

```bash
uv sync
uv run app          # Flask API on flask_port
```

### Usage report

```bash
bash sync.sh pull                                    # copy the DB down from the Pi
uv run weekly-report                                 # all data -> tmp/weekly-report.html
uv run weekly-report --since 2026-07-20 --out ~/Desktop/claude-usage.html
```

Writes one HTML file with no dependencies. It opens in cost view, with a switch to tokens. Sections:
work vs personal per week, new work vs cache reads, model mix, dollars per 1% of the weekly limit,
a weekday × hour heatmap, session cost spread with the most expensive sessions, and top projects and
skills.

- **Each `request_id` counts once.** Claude Code writes several transcript entries per API response
  and repeats the usage on each, so adding up raw rows roughly doubles the totals.
- **Work vs personal comes from the working directory.** A request is work when its `cwd` contains a
  path from `report_work_paths` in `pyproject.toml`. Old paths stay in that list after a repo moves,
  or its history flips to personal.
- **Costs are list-price estimates**, from the `_RATES` table in `src/weekly_report.py`. The command
  warns about any model missing from it and counts that model as $0.
- **The 1-hour cache-write rate (2× input) is unverified.** Most cache writes use the 1-hour cache,
  so this one number moves the cost totals noticeably.
- **Weekly limit use adds up rises between readings.** Anthropic sometimes resets the weekly limit
  early without changing `resets_at`, so the peak reading alone undercounts those weeks.

## Architecture

```
ClaudeUsageNotch app ──POST /api/records──► Flask API ──► SQLite (usage_records)
        ▲                                       │
        └──────────GET /api/analytics──────────►┘  (aggregated in src/analytics.py)
```

Tables are created on startup via SQLAlchemy `create_all`. There's no migration tooling:
it's a single-user store, and the rare schema change ships as a one-off script.

| File | Role |
|------|------|
| `src/app.py` | Flask app factory + entry point (`uv run app`); schedules the daily 05:30 backfill in-process via APScheduler |
| `src/routes.py` | The endpoints below |
| `src/models.py` | `UsageRecord` / `QuotaSnapshot` / `UsageStats` models + JSON parsing |
| `src/analytics.py` | On-demand aggregation for `/api/analytics` (cost, breakdowns, buckets) |
| `src/database.py` | Engine, `session_scope`, `init_db` |
| `src/config.py` | All config (read from `pyproject.toml`) + `DATABASE_URL`; CLI for install scripts |
| `src/ingest.py` | `insert_usage_records`: collapse lines to one row per request, upsert, move lifetime cost by the change. Shared by `POST /api/records` and the backfill |
| `src/backfill.py` | `uv run backfill`: load this machine's `~/.claude/projects/**/*.jsonl` straight into the DB |
| `src/weekly_report.py` | `uv run weekly-report`: usage and cost report, rendered into `src/weekly_report.html` |
| `src/telemetry.py` | Self-observability: `initialize()`s this service against [Spyglass](https://github.com/momonala/spyglass) once per process, exporting `logger`/`metrics` |

## Observability

This service reports its own operational metrics and logs to a [Spyglass](https://github.com/momonala/spyglass)
server (see `src/telemetry.py`), separate from the usage data it stores about *other*
Claude Code sessions. `src/telemetry.py` is imported once per process entry point
(`app.py`) — the import calls `spyglass.initialize()` exactly once, which attaches
a log-shipping handler to the root logger and creates the shared `metrics`
collector; every module in that process gets remote log shipping for free via
propagation, and imports `metrics` from `src.telemetry` when it needs to emit a
counter or timing. Don't call `initialize()` a second time within the same process —
it isn't idempotent and would attach a duplicate log handler.

Metrics emitted (stat names auto-prefixed `claude-usage-notch-server.{function}.*`):

| Stat | Where | Meaning |
|------|-------|---------|
| `post_records.records_inserted` | `routes.py` | `/api/records` ingest volume |
| `post_quota_snapshots.quota_snapshots_inserted` | `routes.py` | `/api/quota_snapshots` ingest volume |
| `get_analytics.duration` | `routes.py` | `/api/analytics` latency (full-table scan for `lifetime_cost` on every call) |
| `backfill.records_inserted` | `backfill.py` | New records the daily backfill found on this machine |

## API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/status` | GET | Health check → `{"status": "ok"}` |
| `/api/records` | POST | Upsert a batch of JSONL lines as one row per request (idempotent by `request_id`) |
| `/api/quota_snapshots` | POST | Upsert a batch of polled quota readings (idempotent by `window_type`+`timestamp`) |
| `/api/analytics` | GET | Pre-aggregated chart data for the session/weekly/month + lookback windows |

### POST /api/records

Body is a JSON **array** of JSONL-derived records. Lines sharing a `request_id` are
collapsed to the one with the most `output_tokens` (early streamed lines carry a partial
count) and the earliest `timestamp`. A stored request is only updated when a later
line has more output, so re-posting a batch is safe. Records without a `request_id`
(Claude Code's zero-token `<synthetic>` lines) are skipped.

```bash
curl -X POST http://localhost:5014/api/records \
  -H 'Content-Type: application/json' \
  -d '[{"request_id":"req_011C...","session_id":"d8e2...","timestamp":"2026-06-09T12:34:54.292Z","cwd":"/Users/me/code/Proj","git_branch":"main","model":"claude-sonnet-4-6","is_sidechain":false,"input_tokens":2,"output_tokens":183,"cache_creation_tokens":7684,"cache_read_tokens":12081,"ephemeral_1h_tokens":7684}]'
```

Response: `{"inserted": N, "updated": U, "skipped": M}`.

### POST /api/quota_snapshots

Body is a JSON **array** of polled quota readings — one per window the client just
fetched from the provider. `window_type` is the app's own window name
(`session`, `weekly`, `weeklyModel`, `monthly`), which is what
`GET /api/analytics` filters on. Upsert is
`INSERT … ON CONFLICT(window_type, timestamp) DO NOTHING`, so re-posting (or two
devices polling the same account a moment apart) is safe. A reading is only stored
when its value or reset time changed, or `QUOTA_HEARTBEAT_INTERVAL` (10 min) passed
since the last stored one for that window; the rest count as `skipped`.

```bash
curl -X POST http://localhost:5014/api/quota_snapshots \
  -H 'Content-Type: application/json' \
  -d '[{"window_type":"session","timestamp":"2026-06-16T12:34:00.000Z","percent_used":0.42,"resets_at":"2026-06-16T17:00:00.000Z"}]'
```

Response: `{"inserted": N, "skipped": M}`.

### GET /api/analytics

Aggregates records into the chart payload the app renders. The five `*_since` params are
required ISO8601 timestamps marking the start of each window; `granularity` is optional:

```bash
curl 'http://localhost:5014/api/analytics?session_since=2026-06-12T07:00:00Z&session_cost_since=2026-06-12T11:00:00Z&weekly_since=2026-06-06T00:00:00Z&month_since=2026-05-13T00:00:00Z&lookback_since=2026-05-13T00:00:00Z&granularity=day'
```

`session_since` is a wide 24h window so `session_buckets` can show the 5h rolling session
resetting several times across the chart — it does **not** drive the "Session" cost pill.
`session_cost_since` is the actual current 5h session start and drives `session_cost`
exclusively; conflating the two let "Session" pick up spend from outside the real window
and exceed "Today". `weekly_since` / `month_since` are fixed reference windows (7d / 30d)
driving the remaining cost pills and the weekly chart. `lookback_since` follows the app's
1D/7D/30D/All selector and drives the period-labeled breakdowns and spend/sessions series
— keep it independent of the fixed windows so, e.g., switching to 7D doesn't shrink the
"Month" figure.

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

One row per Claude API request. `request_id` (primary key) and `timestamp` are
required; everything else is nullable or defaults to 0. `timestamp` is the request's
first line. The project shown in breakdowns is `cwd`'s last path component.

```
request_id, timestamp, session_id, cwd, git_branch, model, attribution_skill,
is_sidechain, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
ephemeral_1h_tokens
```

### Quota snapshot schema

`window_type` + `timestamp` form the composite primary key (`WITHOUT ROWID`).
`percent_used` is required (0...1+); `resets_at` is optional.

```
window_type, timestamp, percent_used, resets_at, source
```

## Deployment

`install/install.sh` installs a systemd service on the Raspberry Pi
(`projects_claude-usage-notch-server.service`) and registers a Cloudflare
tunnel route at `claude-usage-notch-server.mnalavadi.org`.
