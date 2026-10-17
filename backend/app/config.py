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
    llm_requests_per_minute: int
    llm_max_retries: int
    public_demo: bool = False
    demo_llm_runs_per_hour: int = 20
    demo_llm_runs_per_visitor_per_hour: int = 5
    demo_max_investigations: int = 4
    demo_reset_cooldown_s: float = 30.0

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
        llm_requests_per_minute=int(os.getenv("LLM_REQUESTS_PER_MINUTE", "0")),
        llm_max_retries=int(os.getenv("LLM_MAX_RETRIES", "4")),
        public_demo=os.getenv("PUBLIC_DEMO", "").strip().lower() in ("1", "true", "yes"),
        demo_llm_runs_per_hour=int(os.getenv("DEMO_LLM_RUNS_PER_HOUR", "20")),
        demo_llm_runs_per_visitor_per_hour=int(os.getenv("DEMO_LLM_RUNS_PER_VISITOR_PER_HOUR", "5")),
        demo_max_investigations=int(os.getenv("DEMO_MAX_INVESTIGATIONS", "4")),
        demo_reset_cooldown_s=float(os.getenv("DEMO_RESET_COOLDOWN_S", "30")),
    )
