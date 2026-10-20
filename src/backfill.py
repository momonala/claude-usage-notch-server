"""Backfill usage_records from all local ~/.claude/projects/**/*.jsonl files.

Parses JSONL the same way LocalHistoryReader.swift does and writes straight to the
local DB. Idempotent: records are deduped by uuid. `app.py` runs it daily at 05:30 to
pick up Claude Code sessions on this machine, which no Mac client sees.

Usage:
    uv run backfill
"""

import json
import logging
from pathlib import Path

from src.ingest import insert_usage_records
from src.telemetry import metrics

logger = logging.getLogger(__name__)

_CLAUDE_DIR = Path.home() / ".claude" / "projects"
_BATCH_SIZE = 1000


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
    seen: set[str] = set()
    records: list[dict] = []
    for f in claude_dir.rglob("*.jsonl"):
        try:
            text = f.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("backfill: skip %s: %s", f, e)
            continue
        for line in text.splitlines():
            record = parse_assistant_line(line)
            if record and record["uuid"] not in seen:
                seen.add(record["uuid"])
                records.append(record)
    return records


def backfill() -> int:
    """Insert every local record the DB doesn't have yet; return how many were new."""
    records = load_all_records(_CLAUDE_DIR)
    inserted = sum(
        insert_usage_records(records[i : i + _BATCH_SIZE])["inserted"]
        for i in range(0, len(records), _BATCH_SIZE)
    )
    metrics.increment("records_inserted", inserted)
    logger.info("backfill: parsed=%d inserted=%d", len(records), inserted)
    return inserted


def main() -> None:
    backfill()
