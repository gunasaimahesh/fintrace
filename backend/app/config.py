"""Runtime settings read from the environment (and `.env` when present)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "backend" / "data"
FRONTEND_DIST = ROOT / "frontend" / "dist"

load_dotenv(ROOT / ".env")

PLACEHOLDER_KEYS = {"", "your_api_key_here"}


@dataclass(frozen=True)
class Settings:
    falkordb_host: str
    falkordb_port: int
    falkordb_username: str | None
    falkordb_password: str | None
    default_tenant: str
    llm_api_key: str | None
    llm_base_url: str | None
    llm_model: str

    @property
    def llm_enabled(self) -> bool:
        return self.llm_api_key is not None


@lru_cache
def get_settings() -> Settings:
    key = os.getenv("LLM_API_KEY", "").strip()
    return Settings(
        falkordb_host=os.getenv("FALKORDB_HOST", "localhost"),
        falkordb_port=int(os.getenv("FALKORDB_PORT", "6379")),
        falkordb_username=os.getenv("FALKORDB_USERNAME") or None,
        falkordb_password=os.getenv("FALKORDB_PASSWORD") or None,
        default_tenant=os.getenv("DEFAULT_TENANT", "acme-pay"),
        llm_api_key=None if key in PLACEHOLDER_KEYS else key,
        llm_base_url=os.getenv("LLM_BASE_URL") or None,
        llm_model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
    )
