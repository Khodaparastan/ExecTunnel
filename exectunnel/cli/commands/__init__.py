# Import plain functions — not Typer sub-apps.
from .config import app as config_app  # config keeps its sub-app (show/validate)
from .connect import connect
from .status import status
from .ws import ws

__all__ = ["connect", "config_app", "status", "ws"]
