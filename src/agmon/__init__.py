"""agmon — remote agent-run monitor: ingester + query API + CLI."""

from .api import create_app, main

__version__ = "0.1.0"
__all__ = ["create_app", "main", "__version__"]
