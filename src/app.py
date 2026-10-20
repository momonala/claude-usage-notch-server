"""Flask app factory and entry point.

Run locally:  uv run app
Served by systemd in production (see install/).
"""

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask

from src.backfill import backfill
from src.config import FLASK_HOST
from src.config import FLASK_PORT
from src.database import init_db
from src.routes import bp
from src.telemetry import logger as _telemetry_logger  # noqa: F401  (wires stdout + Spyglass logging)

logging.getLogger("werkzeug").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)


def create_app() -> Flask:
    app = Flask(__name__)
    app.register_blueprint(bp)
    init_db()
    scheduler = BackgroundScheduler()
    scheduler.add_job(backfill, "cron", hour=5, minute=30)
    scheduler.start()
    return app


def main():
    app = create_app()
    logger = logging.getLogger(__name__)
    logger.info("Starting server at http://%s:%s", FLASK_HOST, FLASK_PORT)
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=False)


if __name__ == "__main__":
    main()
