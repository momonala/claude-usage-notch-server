"""Periodic task runner for the Claude Usage Notch server.

Runs in-process as an APScheduler `BackgroundScheduler`, started by
`create_app()` when the Flask app boots (see `app.py`) — there is no separate
scheduler process or systemd unit.

Tasks:
  - quota_poll  (every 5 min)  — runs `claude -p /usage`, parses quota %, POSTs to /api/quota_snapshots
  - backfill    (daily 05:30) — runs `uv run backfill` to catch records the Mac missed
"""

import logging
import re
import shutil
import socket
import subprocess
from datetime import datetime
from datetime import timezone

import requests
from apscheduler.schedulers.background import BackgroundScheduler

from src.config import FLASK_PORT
from src.telemetry import metrics

logger = logging.getLogger(__name__)

_USAGE_RE = re.compile(
    r"Current (?P<label>[^:]+):\s+(?P<pct>\d+)%\s+used(?:\s+·\s+resets\s+(?P<resets>.+?)(?:\s+\(|$))?"
)
_WINDOW_MAP = {"session": "five_hour", "week (all models)": "seven_day"}
_SUBSCRIPTION_ONLY_MARKER = "You are currently using your subscription to power your Claude Code usage"

_SERVER = f"http://localhost:{FLASK_PORT}"
SOURCE = socket.gethostname()

_QUOTA_POLL_INTERVAL_MINUTES = 5
_BACKFILL_TIME = (5, 30)
_TIMEOUT_SECONDS = 60


def _is_per_model_week_label(label: str) -> bool:
    """True for per-model weekly caps such as 'week (fable)'.

    Only the aggregate 'week (all models)' maps to a stored window type. Claude also
    reports a cap per model, which is expected and skipped without warning.
    """
    return label.startswith("week (")


def _parse_resets_at(raw: str) -> str | None:
    """Parse 'Jun 17, 12:40pm' or 'Jun 17 at 12:40pm' into an ISO-8601 UTC string best-effort."""
    for fmt in ("%b %d, %I:%M%p", "%b %d, %I%p", "%b %d at %I:%M%p", "%b %d at %I%p"):
        try:
            naive = datetime.strptime(raw.strip(), fmt)
            # Attach current year; server stores as UTC (close enough for a daily reset marker)
            dt = naive.replace(year=datetime.now().year, tzinfo=timezone.utc)
            return dt.isoformat()
        except ValueError:
            continue
    return None


def _is_subscription_only_output(output: str) -> bool:
    """Return True when /usage returned the subscription banner but no quota lines."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return len(lines) == 1 and lines[0] == _SUBSCRIPTION_ONLY_MARKER


def _log_claude_failure(task: str, result: subprocess.CompletedProcess[str]) -> None:
    logger.error(
        "%s: claude exited %d\nstdout: %s\nstderr: %s",
        task,
        result.returncode,
        result.stdout.strip(),
        result.stderr.strip(),
    )


def poll_quota() -> None:
    with metrics.timed("duration"):
        result = subprocess.run(
            [
                "claude",
                "--strict-mcp-config",
                "--no-session-persistence",
                "--permission-mode",
                "bypassPermissions",
                "-p",
                "/usage",
            ],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
        )
    if result.returncode != 0:
        _log_claude_failure("quota_poll", result)
        metrics.increment("claude_error")
        return

    now = datetime.now(timezone.utc).isoformat()
    output = result.stdout + "\n" + result.stderr
    records = []
    for match in _USAGE_RE.finditer(output):
        label = match.group("label").strip().lower()
        window_type = _WINDOW_MAP.get(label)
        if not window_type:
            if not _is_per_model_week_label(label):
                logger.warning("quota_poll: unrecognised label %r — skipping", label)
                metrics.increment("unrecognised_label")
            continue
        resets_raw = match.group("resets")
        records.append(
            {
                "window_type": window_type,
                "percent_used": float(match.group("pct")),
                "resets_at": _parse_resets_at(resets_raw) if resets_raw else None,
                "source": SOURCE,
                "timestamp": now,
            }
        )

    if not records:
        if _is_subscription_only_output(output):
            logger.debug("quota_poll: subscription-only /usage output (no quota lines) — skipping")
            return
        logger.warning(
            "quota_poll: no quota lines matched — the `claude /usage` output format may have "
            "changed.\nExpected lines like: 'Current session: 42%% used'\nGot:\n%s",
            output.strip(),
        )
        metrics.increment("no_quota_lines_matched")
        return

    resp = requests.post(f"{_SERVER}/api/quota_snapshots", json=records, timeout=10)
    resp.raise_for_status()
    logger.debug("quota_poll: %s", resp.json())
    metrics.increment("success")


def backfill() -> None:
    uv_bin = shutil.which("uv") or "/home/mnalavadi/.local/bin/uv"
    result = subprocess.run(
        [uv_bin, "run", "backfill"],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
    )
    if result.stdout.strip():
        logger.info("backfill: stdout:\n%s", result.stdout.rstrip())
    if result.returncode != 0:
        if result.stderr.strip():
            logger.error("backfill: stderr:\n%s", result.stderr.rstrip())
        logger.error("backfill: exited with code %d", result.returncode)
        metrics.increment("failure")
    else:
        logger.info("backfill: ok")
        metrics.increment("success")


def start() -> BackgroundScheduler:
    """Schedule quota polling + the daily backfill and start running them in the background."""
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        poll_quota,
        "interval",
        minutes=_QUOTA_POLL_INTERVAL_MINUTES,
        next_run_time=datetime.now(),
    )
    scheduler.add_job(backfill, "cron", hour=_BACKFILL_TIME[0], minute=_BACKFILL_TIME[1])
    scheduler.start()
    logger.info("scheduler started (source=%s, server=%s)", SOURCE, _SERVER)
    return scheduler
