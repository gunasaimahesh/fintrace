"""Guard rails for running FinTrace as a public demo.

Visitors share each tenant's graph (demo data isolation, not access control): their changes are applied one at a
time and everyone sees them. A reset waits until no investigation is using the graph. With PUBLIC_DEMO set, model
use is capped per hour, overall and per visitor, and past the cap an investigation runs on the deterministic
planner instead of being refused; concurrent investigations are bounded, and resets are spaced out.
"""

from __future__ import annotations

import math
import time
from collections import Counter, deque
from contextlib import contextmanager

from backend.app.config import get_settings

HOUR_S = 3600.0


def _runs(n: int) -> str:
    return f"{n} run" if n == 1 else f"{n} runs"


class Window:
    """At most `limit` uses in any window of `seconds`."""

    def __init__(self, limit: int, seconds: float = HOUR_S):
        self.limit, self.seconds, self.hits = limit, seconds, deque()

    def left(self) -> int:
        now = time.monotonic()
        while self.hits and now - self.hits[0] >= self.seconds:
            self.hits.popleft()
        return max(0, self.limit - len(self.hits))

    def take(self) -> None:
        self.hits.append(time.monotonic())


class Guard:
    def __init__(self) -> None:
        self.running: Counter[str] = Counter()
        self.last_reset: dict[str, float] = {}
        self.llm: Window | None = None
        self.visitors: dict[str, Window] = {}

    def _llm(self) -> Window:
        if self.llm is None:
            self.llm = Window(get_settings().demo_llm_runs_per_hour)
        return self.llm

    def planner(self, visitor: str, mode: str) -> tuple[str, str | None]:
        """The planner this visitor's investigation may use, with a notice when the model cap downgraded it."""
        settings = get_settings()
        if not settings.public_demo or not settings.llm_enabled or mode == "fallback":
            return mode, None
        mine = self.visitors.setdefault(visitor, Window(settings.demo_llm_runs_per_visitor_per_hour))
        if not mine.left():
            return "fallback", (f"You have used your model runs for this hour ({_runs(mine.limit)} per visitor), so "
                                "this run uses the deterministic planner.")
        if not self._llm().left():
            return "fallback", (f"The demo's model runs for this hour ({_runs(self._llm().limit)} across all "
                                "visitors) are used up, so this run uses the deterministic planner.")
        mine.take()
        self._llm().take()
        for name in [v for v, w in self.visitors.items() if w.left() == w.limit]:
            del self.visitors[name]
        return mode, None

    def busy(self) -> bool:
        settings = get_settings()
        return settings.public_demo and sum(self.running.values()) >= settings.demo_max_investigations

    @contextmanager
    def investigation(self, key: str):
        self.running[key] += 1
        try:
            yield
        finally:
            self.running[key] -= 1
            if not self.running[key]:
                del self.running[key]

    def refuse_reset(self, key: str) -> tuple[int, str] | None:
        """Why the graph can't be reset right now, as an HTTP status and message; None when it can."""
        if self.running[key]:
            return 409, "an investigation is using this tenant's graph; reset when it finishes"
        settings = get_settings()
        wait = settings.demo_reset_cooldown_s - (time.monotonic() - self.last_reset.get(key, -math.inf))
        if settings.public_demo and wait > 0:
            return 429, f"this tenant was just reset; try again in {math.ceil(wait)} s"
        return None

    def reset_done(self, key: str) -> None:
        self.last_reset[key] = time.monotonic()

    def status(self) -> dict | None:
        settings = get_settings()
        if not settings.public_demo:
            return None
        return {"llm_runs_per_hour": settings.demo_llm_runs_per_hour,
                "llm_runs_per_visitor_per_hour": settings.demo_llm_runs_per_visitor_per_hour,
                "llm_runs_left": self._llm().left() if settings.llm_enabled else 0,
                "max_investigations": settings.demo_max_investigations, "hunter_planner": "fallback"}


guard = Guard()
