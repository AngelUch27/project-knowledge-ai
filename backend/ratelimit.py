"""In-process sliding-window rate limits for the expensive routes.

Every question costs Agent Search queries and Gemini calls in a shared GCP
project, and every import costs an indexing operation. The limits are wide
enough never to bother a person using the app, and tight enough to stop a
script hammering it. Each policy may have several windows (per minute and
per hour); a request is allowed only when all of them have room.

The limiter warns before it blocks: ``Decision.warning`` is set once any
window is at least ``WARN_RATIO`` full, so callers can tell the user (and the
server log) that the limit is near.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

WARN_RATIO = 0.8


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, "") or default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Window:
    limit: int
    seconds: int

    @property
    def label(self) -> str:
        return {60: "minute", 3600: "hour", 86400: "day"}.get(self.seconds, f"{self.seconds}s")


@dataclass(frozen=True)
class Decision:
    allowed: bool
    policy: str
    used: int          # requests in the tightest window (the one closest to its limit)
    limit: int
    window: str
    retry_after: int   # seconds until a slot frees up (0 when allowed)
    warning: bool      # at or above WARN_RATIO in any window

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def headers(self) -> dict[str, str]:
        headers = {
            "X-RateLimit-Policy": self.policy,
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(self.remaining),
            "X-RateLimit-Window": self.window,
        }
        if self.warning:
            headers["X-RateLimit-Warning"] = f"{self.used}/{self.limit} per {self.window}"
        if not self.allowed:
            headers["Retry-After"] = str(self.retry_after)
        return headers

    def usage(self) -> dict[str, object]:
        return {"policy": self.policy, "used": self.used, "limit": self.limit, "window": self.window, "warning": self.warning}


DEFAULT_POLICIES: dict[str, tuple[Window, ...]] = {
    # questions: Agent Search + Gemini per call
    "ask": (Window(_env_int("RATE_LIMIT_ASK_PER_MINUTE", 20), 60), Window(_env_int("RATE_LIMIT_ASK_PER_HOUR", 200), 3600)),
    "search": (Window(60, 60), Window(600, 3600)),
    # imports and syncs: each one is an indexing operation and GitHub/Graph traffic
    "import": (Window(_env_int("RATE_LIMIT_IMPORTS_PER_HOUR", 12), 3600),),
    # file uploads
    "upload": (Window(30, 60), Window(_env_int("RATE_LIMIT_UPLOADS_PER_HOUR", 200), 3600)),
}


class SlidingWindowLimiter:
    def __init__(self, policies: dict[str, tuple[Window, ...]] | None = None, clock=time.monotonic) -> None:
        self._policies = policies or DEFAULT_POLICIES
        self._clock = clock
        self._events: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._warned: set[tuple[str, str]] = set()

    def policy(self, name: str) -> tuple[Window, ...]:
        return self._policies[name]

    def check(self, policy: str, key: str = "local", *, record: bool = True) -> Decision:
        """Decide whether one more request under ``policy`` for ``key`` is allowed; record it if so."""
        windows = self._policies[policy]
        now = self._clock()
        horizon = max(w.seconds for w in windows)
        with self._lock:
            events = self._events[(policy, key)]
            while events and now - events[0] >= horizon:
                events.popleft()

            tightest, blocked_by = None, None
            for window in windows:
                used = sum(1 for t in events if now - t < window.seconds)
                ratio = used / window.limit
                if tightest is None or ratio > tightest[1]:
                    tightest = (window, ratio, used)
                if used >= window.limit and blocked_by is None:
                    oldest_in_window = next(t for t in events if now - t < window.seconds)
                    blocked_by = (window, used, int(window.seconds - (now - oldest_in_window)) + 1)

            if blocked_by is not None:
                window, used, retry = blocked_by
                logger.warning("Rate limit reached: %s %d/%d per %s (key=%s); retry in %ss", policy, used, window.limit, window.label, key, retry)
                return Decision(False, policy, used, window.limit, window.label, retry, True)

            window, ratio, used = tightest
            if record:
                events.append(now)
                used += 1
                ratio = used / window.limit
            warning = ratio >= WARN_RATIO
            if warning and (policy, key) not in self._warned:
                self._warned.add((policy, key))
                logger.warning("Rate limit nearly reached: %s %d/%d per %s (key=%s)", policy, used, window.limit, window.label, key)
            elif not warning:
                self._warned.discard((policy, key))
            return Decision(True, policy, used, window.limit, window.label, 0, warning)

    def snapshot(self, key: str = "local") -> list[dict[str, object]]:
        """Current usage of every policy without recording anything."""
        return [self.check(name, key, record=False).usage() for name in self._policies]
