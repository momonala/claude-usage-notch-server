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
from datetime import datetime
from datetime import timezone
from datetime import timedelta
import time

import requests
import schedule
import typer
from dotenv import load_dotenv

from src.config import FLASK_PORT

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_USAGE_RE = re.compile(
    r"Current (?P<label>[^:]+):\s+(?P<pct>\d+)%\s+used\s+·\s+resets\s+(?P<resets>.+?)(?:\s+\(|$)"
)
_WINDOW_MAP = {"session": "five_hour", "week (all models)": "seven_day"}

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


def _resolve_server(server: str, prod: bool) -> str:
    if prod:
        prod_url = os.environ.get("PROD_URL")
        if not prod_url:
            typer.secho("Error: PROD_URL not set in .env", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)
        return prod_url
    return server


def poll_quota(server: str) -> None:
    result = subprocess.run(
        ["claude", "--permission-mode", "bypassPermissions", "-p", "/usage"],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        check=True,
    )

    now = datetime.now(timezone.utc).isoformat()
    records = []
    for match in _USAGE_RE.finditer(result.stdout):
        label = match.group("label").strip().lower()
        window_type = _WINDOW_MAP.get(label)
        if not window_type:
            logger.warning("quota_poll: unrecognised label %r — skipping", label)
            continue
        records.append(
            {
                "window_type": window_type,
                "percent_used": float(match.group("pct")),
                "resets_at": _parse_resets_at(match.group("resets")),
                "source": SOURCE,
                "timestamp": now,
            }
        )

    if not records:
        raise ValueError(f"no quota lines parsed from output:\n{result.stdout}")

    resp = requests.post(f"{server.rstrip('/')}/api/quota_snapshots", json=records, timeout=10)
    resp.raise_for_status()
    logger.info("quota_poll: %s", resp.json())


def daily_ping(prod: bool = False) -> None:
    ping = subprocess.run(
        ["claude", "--permission-mode", "bypassPermissions", "--model", "haiku", "ping reply in one word"],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        check=True,
    )
    logger.info("daily_ping: ping ok — %s", ping.stdout.strip())

    backfill_cmd = ["uv", "run", "backfill"]
    if prod:
        backfill_cmd.append("--prod")

    backfill = subprocess.run(
        backfill_cmd,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        check=True,
    )
    if backfill.stdout.strip():
        logger.info("daily_ping: backfill stdout:\n%s", backfill.stdout.rstrip())
    if backfill.stderr.strip():
        logger.info("daily_ping: backfill stderr:\n%s", backfill.stderr.rstrip())
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
    schedule.every().day.at(_FIRST_DAILY_PING_TIME.strftime("%H:%M")).do(daily_ping, server)
    schedule.every().day.at(_SECOND_DAILY_PING_TIME.strftime("%H:%M")).do(daily_ping, server)

    poll_quota(server)

    while True:
        schedule.run_pending()
        time.sleep(10)


def main() -> None:
    typer.run(scheduler_cli)


if __name__ == "__main__":
    main()
