"""
Periodic task runner for the Claude Usage Notch server.

Tasks:
  - quota_poll  (every 5 min)  — runs `claude -p /usage`, parses quota %, POSTs to /api/quota_snapshots
  - daily_ping  (every day)    — keeps Pro subscription alive + runs backfill
"""

import logging
import os
import re
import socket
import subprocess
import time
from datetime import datetime
from datetime import timedelta
from datetime import timezone

import requests
import schedule
import typer
from dotenv import load_dotenv

from src.config import FLASK_PORT

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_USAGE_RE = re.compile(
    r"Current (?P<label>[^:]+):\s+(?P<pct>\d+)%\s+used(?:\s+·\s+resets\s+(?P<resets>.+?)(?:\s+\(|$))?"
)
_WINDOW_MAP = {"session": "five_hour", "week (all models)": "seven_day"}
_SUBSCRIPTION_ONLY_MARKER = "You are currently using your subscription to power your Claude Code usage"

_DEFAULT_SERVER = f"http://localhost:{FLASK_PORT}"
SOURCE = socket.gethostname()

_QUOTA_POLL_INTERVAL_MINUTES = 2
now = datetime.now()
_FIRST_DAILY_PING_TIME = now.replace(hour=5, minute=30)
_SECOND_DAILY_PING_TIME = _FIRST_DAILY_PING_TIME + timedelta(hours=5, minutes=1)
_TIMEOUT_SECONDS = 60


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


def _resolve_server(server: str, prod: bool) -> str:
    if prod:
        prod_url = os.environ.get("PROD_URL")
        if not prod_url:
            typer.secho("Error: PROD_URL not set in .env", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)
        return prod_url
    return server


def _log_claude_failure(task: str, result: subprocess.CompletedProcess[str]) -> None:
    logger.error(
        "%s: claude exited %d\nstdout: %s\nstderr: %s",
        task,
        result.returncode,
        result.stdout.strip(),
        result.stderr.strip(),
    )


def poll_quota(server: str) -> None:
    result = subprocess.run(
        ["claude", "--permission-mode", "bypassPermissions", "-p", "/usage"],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        _log_claude_failure("quota_poll", result)
        return

    now = datetime.now(timezone.utc).isoformat()
    output = result.stdout + "\n" + result.stderr
    records = []
    for match in _USAGE_RE.finditer(output):
        label = match.group("label").strip().lower()
        window_type = _WINDOW_MAP.get(label)
        if not window_type:
            logger.warning("quota_poll: unrecognised label %r — skipping", label)
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
        return

    resp = requests.post(f"{server.rstrip('/')}/api/quota_snapshots", json=records, timeout=10)
    resp.raise_for_status()
    logger.debug("quota_poll: %s", resp.json())


def daily_ping(prod: bool = False) -> None:
    ping = subprocess.run(
        [
            "claude",
            "--permission-mode",
            "bypassPermissions",
            "--model",
            "haiku",
            "-p",
            "ping reply in one word",
        ],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
    )
    if ping.returncode != 0:
        _log_claude_failure("daily_ping", ping)
    else:
        logger.info("daily_ping: ping ok — %s", ping.stdout.strip())

    backfill_cmd = ["uv", "run", "backfill"]
    if prod:
        backfill_cmd.append("--prod")

    backfill = subprocess.run(
        backfill_cmd,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
    )
    if backfill.stdout.strip():
        logger.info("daily_ping: backfill stdout:\n%s", backfill.stdout.rstrip())
    if backfill.stderr.strip():
        logger.info("daily_ping: backfill stderr:\n%s", backfill.stderr.rstrip())
    if backfill.returncode != 0:
        logger.error("daily_ping: backfill exited with code %d", backfill.returncode)
    else:
        logger.info("daily_ping: backfill ok")


def scheduler_cli(
    server: str = typer.Option(_DEFAULT_SERVER, "--server", help="Sync server base URL"),
    prod: bool = typer.Option(False, "--prod", help="Target production server (PROD_URL from .env)"),
) -> None:
    """Periodic task runner for quota polling and daily pings."""
    server = _resolve_server(server, prod)
    typer.secho(f"Target server: {server}", fg=typer.colors.MAGENTA, bold=True)
    logger.info("scheduler starting (source=%s, server=%s)", SOURCE, server)

    schedule.every(_QUOTA_POLL_INTERVAL_MINUTES).minutes.do(poll_quota, server)
    schedule.every().day.at(_FIRST_DAILY_PING_TIME.strftime("%H:%M")).do(daily_ping, prod)
    schedule.every().day.at(_SECOND_DAILY_PING_TIME.strftime("%H:%M")).do(daily_ping, prod)

    poll_quota(server)

    while True:
        schedule.run_pending()
        time.sleep(10)


def main() -> None:
    typer.run(scheduler_cli)


if __name__ == "__main__":
    main()
