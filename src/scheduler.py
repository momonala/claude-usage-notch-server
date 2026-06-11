"""Entry point for periodic background tasks.

Currently just the hourly DB git backup. Future scheduled work (pre-aggregated
views, cleanup jobs) gets registered here.

Run locally:  uv run scheduler
Served by systemd in production (see install/).
"""

import logging
import time

import schedule

from src.git_tool import commit_db_if_changed

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def get_scheduled_jobs() -> list[str]:
    """Return the scheduled jobs, for logging/inspection."""
    return [repr(job) for job in schedule.get_jobs()]


def main():
    schedule.every().hour.at(":00").do(commit_db_if_changed)
    logger.info("Scheduled hourly commit of DB if changed")
    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
