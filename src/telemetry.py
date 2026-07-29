"""Spyglass wiring for this service.

Importing this module configures remote log shipping (attaches a handler to the
root logger) and returns a metrics collector. Import it once per process entry
point (app.py, scheduler.py) — importing it again elsewhere is safe since Python
caches modules, but calling `initialize()` directly a second time would attach a
duplicate log handler and double-ship every log line.
"""

from spyglass import initialize

from src.config import PROJECT_NAME
from src.config import SPYGLASS_HOST

logger, metrics = initialize(host=SPYGLASS_HOST, project=PROJECT_NAME)
