"""Flask app factory and entry point.

Run locally:  uv run app
Served by systemd in production (see install/).
"""

import logging

from flask import Flask

from src.config import FLASK_HOST
from src.config import FLASK_PORT
from src.database import init_db
from src.routes import bp

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logging.getLogger("werkzeug").setLevel(logging.DEBUG)


def create_app() -> Flask:
    app = Flask(__name__)
    app.register_blueprint(bp)
    init_db()
    return app


def main():
    app = create_app()
    logger = logging.getLogger(__name__)
    logger.info("Starting server at http://%s:%s", FLASK_HOST, FLASK_PORT)
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=False)


if __name__ == "__main__":
    main()
