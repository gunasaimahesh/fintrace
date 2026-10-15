"""Runtime settings read from the environment (and `.env` when present)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "backend" / "data"

load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    falkordb_host: str
    falkordb_port: int
    falkordb_username: str | None
    falkordb_password: str | None
    default_tenant: str


@lru_cache
def get_settings() -> Settings:
    return Settings(
        falkordb_host=os.getenv("FALKORDB_HOST", "localhost"),
        falkordb_port=int(os.getenv("FALKORDB_PORT", "6379")),
        falkordb_username=os.getenv("FALKORDB_USERNAME") or None,
        falkordb_password=os.getenv("FALKORDB_PASSWORD") or None,
        default_tenant=os.getenv("DEFAULT_TENANT", "acme-pay"),
    )
