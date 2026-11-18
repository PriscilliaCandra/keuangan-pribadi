"""Pembatas percobaan (anti tebak kode undangan / token login)."""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Callable, Hashable


class RateLimiter:
    """Maksimal `max_events` kejadian per `window_s` detik untuk tiap kunci.

    Pola pakai untuk percobaan gagal: `blocked(key)` dulu; bila gagal `record(key)`; bila
    berhasil `reset(key)`.
    """

    def __init__(self, max_events: int, window_s: float, now: Callable[[], float] = time.monotonic):
        self.max_events, self.window_s, self._now = max_events, window_s, now
        self._events: dict[Hashable, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: Hashable) -> deque[float]:
        """Buang kejadian kedaluwarsa. Kunci tanpa kejadian dibuang dari memori."""
        q = self._events.get(key)
        if q is None:
            return deque()
        cutoff = self._now() - self.window_s
        while q and q[0] <= cutoff:
            q.popleft()
        if not q:
            del self._events[key]
            return deque()
        return q

    def record(self, key: Hashable) -> None:
        with self._lock:
            self._prune(key)
            self._events[key].append(self._now())

    def blocked(self, key: Hashable) -> bool:
        with self._lock:
            return len(self._prune(key)) >= self.max_events

    def retry_after(self, key: Hashable) -> int:
        """Sisa detik sampai boleh mencoba lagi (0 bila tidak diblokir)."""
        with self._lock:
            q = self._prune(key)
            if len(q) < self.max_events:
                return 0
            return max(1, int(q[0] + self.window_s - self._now()) + 1)

    def reset(self, key: Hashable) -> None:
        with self._lock:
            self._events.pop(key, None)
