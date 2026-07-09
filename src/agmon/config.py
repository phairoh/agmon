"""Configuration, read once from the environment at startup."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    runs_dir: Path
    db_path: Path
    host: str
    port: int
    stall_seconds: int = 300

    @classmethod
    def from_env(cls) -> "Config":
        home = Path.home()
        runs_dir = Path(
            os.environ.get("AGENT_RUNS_DIR", home / "agent-runs")
        ).expanduser()
        db_path = Path(
            os.environ.get("AGMON_DB", home / ".local/share/agmon/agmon.db")
        ).expanduser()
        host = os.environ.get("AGMON_HOST", "0.0.0.0")
        port = int(os.environ.get("AGMON_PORT", "8400"))
        stall_seconds = int(os.environ.get("AGMON_STALL_SECONDS", "300"))
        return cls(
            runs_dir=runs_dir,
            db_path=db_path,
            host=host,
            port=port,
            stall_seconds=stall_seconds,
        )
