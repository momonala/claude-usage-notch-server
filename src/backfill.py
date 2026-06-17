"""Backfill the sync server from all local ~/.claude/projects/**/*.jsonl files.

Parses JSONL the same way LocalHistoryReader.swift does, then POSTs records to
the server in batches. Idempotent — the server dedupes by uuid.

Covers `usage_records` (token counts) only. There is no local source for
`quota_snapshots` (polled session/weekly quota %) — Claude's JSONL history doesn't
carry it, so that table only has data from whenever a client started polling and
pushing it; it can't be backfilled retroactively.

Usage:
    uv run python backfill.py
    uv run python backfill.py --prod
    uv run python backfill.py --server http://localhost:FLASK_PORT

Reads PROD_URL from a .env file in the same directory as this script (optional).
"""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import as_completed
from pathlib import Path

import orjson
import requests
import typer
from dotenv import load_dotenv
from tqdm import tqdm
from src.config import FLASK_PORT

load_dotenv()

_LOCAL_URL = f"http://localhost:{FLASK_PORT}"
_JSON_HEADERS = {"Content-Type": "application/json"}
_WORKERS = 4
_BATCH_SIZE = 1000


def find_jsonl_files(root: Path) -> list[Path]:
    return list(root.rglob("*.jsonl"))


def parse_assistant_line(raw: str) -> dict | None:
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None

    if obj.get("type") != "assistant":
        return None
    if obj.get("isApiErrorMessage") is True:
        return None

    uuid = obj.get("uuid")
    ts_str = obj.get("timestamp")
    if not uuid or not ts_str:
        return None

    message = obj.get("message", {})
    usage = message.get("usage", {})
    server_tool_use = usage.get("server_tool_use", {}) or {}
    cache_creation = usage.get("cache_creation", {}) or {}

    cwd = obj.get("cwd", "")
    project = Path(cwd).name or "unknown"

    return {
        "uuid": uuid,
        "request_id": obj.get("requestId"),
        "session_id": obj.get("sessionId") or "",
        "parent_uuid": obj.get("parentUuid"),
        "timestamp": ts_str,
        "cwd": cwd,
        "project": project,
        "git_branch": obj.get("gitBranch"),
        "model": message.get("model") or "unknown",
        "version": obj.get("version"),
        "entrypoint": obj.get("entrypoint"),
        "attribution_skill": obj.get("attributionSkill"),
        "is_sidechain": bool(obj.get("isSidechain", False)),
        "stop_reason": message.get("stop_reason"),
        "service_tier": usage.get("service_tier"),
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0),
        "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
        "ephemeral_1h_tokens": cache_creation.get("ephemeral_1h_input_tokens", 0),
        "ephemeral_5m_tokens": cache_creation.get("ephemeral_5m_input_tokens", 0),
        "web_searches": server_tool_use.get("web_search_requests", 0),
        "web_fetches": server_tool_use.get("web_fetch_requests", 0),
    }


def load_all_records(claude_dir: Path) -> list[dict]:
    files = find_jsonl_files(claude_dir)
    typer.secho(f"Found {len(files)} JSONL files under {claude_dir}", fg=typer.colors.CYAN)

    seen: set[str] = set()
    records: list[dict] = []

    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except OSError as e:
            typer.secho(f"  skip {f}: {e}", fg=typer.colors.YELLOW)
            continue

        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            record = parse_assistant_line(line)
            if record and record["uuid"] not in seen:
                seen.add(record["uuid"])
                records.append(record)

    return records


def post_batch(server: str, batch: list[dict]) -> tuple[int, int]:
    url = f"{server.rstrip('/')}/api/records"
    with requests.Session() as http:
        resp = http.post(url, data=orjson.dumps(batch), headers=_JSON_HEADERS, timeout=30)
    resp.raise_for_status()
    result = resp.json()
    return result.get("inserted", 0), result.get("skipped", 0)


def backfill_cli(
    server: str = typer.Option(_LOCAL_URL, help="Sync server base URL"),
    prod: bool = typer.Option(False, "--prod", help="Target production server (PROD_URL from .env)"),
    workers: int = typer.Option(_WORKERS, help="Concurrent POST threads"),
) -> None:
    """Backfill usage DB from local JSONL files."""
    if prod:
        prod_url = os.environ.get("PROD_URL")
        if not prod_url:
            typer.secho("Error: PROD_URL not set in .env", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)
        server = prod_url

    typer.secho(f"Target server: {server}", fg=typer.colors.MAGENTA, bold=True)

    claude_dir = Path.home() / ".claude" / "projects"
    if not claude_dir.exists():
        typer.secho(f"Error: {claude_dir} not found", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    records = load_all_records(claude_dir)
    typer.secho(f"Parsed {len(records)} unique assistant records", fg=typer.colors.CYAN, bold=True)

    if not records:
        typer.secho("Nothing to backfill.", fg=typer.colors.YELLOW)
        return

    batches = [records[i : i + _BATCH_SIZE] for i in range(0, len(records), _BATCH_SIZE)]
    total_inserted = total_skipped = 0

    with (
        ThreadPoolExecutor(max_workers=workers) as pool,
        tqdm(total=len(batches), desc="Posting batches", unit="batch") as bar,
    ):
        futures = {pool.submit(post_batch, server, batch): batch for batch in batches}
        for future in as_completed(futures):
            inserted, skipped = future.result()
            total_inserted += inserted
            total_skipped += skipped
            bar.set_postfix(inserted=total_inserted, skipped=total_skipped)
            bar.update(1)

    typer.secho(
        f"\nDone. Total inserted={total_inserted} skipped={total_skipped}",
        fg=typer.colors.GREEN,
        bold=True,
    )


def main() -> None:
    typer.run(backfill_cli)


if __name__ == "__main__":
    main()
