"""Backfill the sync server from all local ~/.claude/projects/**/*.jsonl files.

Parses JSONL the same way LocalHistoryReader.swift does, then POSTs records to
the server in batches. Idempotent — the server dedupes by uuid.

Usage:
    uv run python backfill.py
    uv run python backfill.py --prod
    uv run python backfill.py --server http://raspberrypi.local:5014
    uv run python backfill.py --server http://localhost:5014 --batch-size 500

Reads PROD_URL from a .env file in the same directory as this script (optional).
"""

import json
import math
import os
import urllib.request
from pathlib import Path

import typer
from dotenv import load_dotenv

load_dotenv()


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
    print(f"Found {len(files)} JSONL files under {claude_dir}")

    seen: set[str] = set()
    records: list[dict] = []

    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except OSError as e:
            print(f"  skip {f}: {e}")
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
    body = json.dumps(batch).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "curl/8.7.1",
            "Accept": "*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
    return result.get("inserted", 0), result.get("skipped", 0)


_LOCAL_URL = "http://localhost:5014"


def backfill_cli(
    server: str = typer.Option(_LOCAL_URL, help="Sync server base URL"),
    prod: bool = typer.Option(False, "--prod", help="Target production server (PROD_URL from .env)"),
    batch_size: int = typer.Option(200, help="Records per POST"),
) -> None:
    """Backfill usage DB from local JSONL files."""
    if prod:
        prod_url = os.environ.get("PROD_URL")
        if not prod_url:
            typer.secho("Error: PROD_URL not set in .env", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)
        server = prod_url

    claude_dir = Path.home() / ".claude" / "projects"
    if not claude_dir.exists():
        typer.secho(f"Error: {claude_dir} not found", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    records = load_all_records(claude_dir)
    typer.echo(f"Parsed {len(records)} unique assistant records")

    if not records:
        typer.echo("Nothing to backfill.")
        return

    total_inserted = total_skipped = 0
    batch_count = math.ceil(len(records) / batch_size)

    for i in range(0, len(records), batch_size):
        batch = records[i : i + batch_size]
        batch_num = i // batch_size + 1
        inserted, skipped = post_batch(server, batch)
        total_inserted += inserted
        total_skipped += skipped
        color = typer.colors.GREEN if inserted >= 1 else typer.colors.YELLOW
        typer.secho(f"  Batch {batch_num}/{batch_count}: inserted={inserted} skipped={skipped}", fg=color)

    typer.echo(f"\nDone. Total inserted={total_inserted} skipped={total_skipped}")


def main() -> None:
    typer.run(backfill_cli)


if __name__ == "__main__":
    main()
