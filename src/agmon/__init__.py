"""agmon — remote agent-run monitor: ingester + query API."""

from .api import create_app, main

__all__ = ["create_app", "main"]
