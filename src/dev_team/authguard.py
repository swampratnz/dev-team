"""Bound repeated bad-token auth attempts against the dispatch/dashboard HTTP
surfaces.

``docs/DISPATCH.md``'s access-log section names the exact threat this module
closes: the bounded ``access.jsonl`` journal (:mod:`dev_team.accesslog`)
stops a long-running service from accreting an unbounded file "under
sustained hostile traffic (even just repeated auth misses)" — but nothing
previously bounded the *requests themselves*. ``dispatch.py``'s
``_authenticate`` and ``dashboard.py``'s ``_authorised``/``_tokens_match``
are pure per-request :func:`hmac.compare_digest` comparisons with no
attempt counter, no backoff, no lockout.

:class:`FailedAuthTracker` is a small, dependency-free, in-process failed-
auth tracker: a bounded, lock-serialised, in-memory structure mapping a
**source IP** (never the attempted token or any derivative of it, per
CLAUDE.md section 2) to a deque of recent failure timestamps. It piggy-backs
entirely on the request-handling path the way :class:`~dev_team.accesslog.
AccessLog`'s lock-serialised append already does — no background sweep
thread (see issue #214's "Alternatives considered": pruning-on-read makes
one unnecessary, the same lesson issue #184 applied to dispatch's TTL
auto-purge).
"""

from __future__ import annotations

import collections
import threading
import time
from typing import Callable, Deque, Dict, Optional

#: Failures within this many seconds of each other count toward the same
#: threshold window.
DEFAULT_WINDOW_SECONDS = 60.0

#: Wrong-token requests allowed within the window before a source is locked
#: out. ``0`` disables the guard entirely (an explicit, documented opt-out).
DEFAULT_THRESHOLD = 10

#: How long a tripped source stays locked out once the threshold is reached.
DEFAULT_LOCKOUT_SECONDS = 60.0

#: Distinct source keys tracked at once; mirrors :mod:`dev_team.accesslog`'s
#: own ``MAX_ACCESS_RECORDS`` bound — "bound the pathological case, don't
#: try to be perfect" (see issue #214's "Alternatives considered" #210/#212
#: precedent). Inserting a new key past the cap evicts the single
#: oldest-inserted key first (a plain ``dict`` preserves insertion order —
#: no extra structure needed).
DEFAULT_MAX_TRACKED_SOURCES = 4096


class FailedAuthTracker:
    """Per-source-IP failed-auth counter with a fixed threshold/window/lockout.

    Every method takes an explicit ``now`` (or defaults to the injected
    :attr:`clock`, itself defaulting to :func:`time.monotonic` — immune to
    system clock adjustments, since only durations ever matter here), so
    callers can drive deterministic tests without real ``time.sleep``.

    Guarded by one :class:`threading.Lock`, the same lock-serialisation
    :meth:`~dev_team.accesslog.AccessLog.append` already relies on for
    ``ThreadingHTTPServer``'s concurrent handlers.
    """

    def __init__(
        self,
        *,
        threshold: int = DEFAULT_THRESHOLD,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        lockout_seconds: float = DEFAULT_LOCKOUT_SECONDS,
        max_tracked_sources: int = DEFAULT_MAX_TRACKED_SOURCES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.threshold = threshold
        self.window_seconds = window_seconds
        self.lockout_seconds = lockout_seconds
        self.max_tracked_sources = max_tracked_sources
        self.clock = clock
        self._failures: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        """Number of distinct source keys currently tracked."""

        with self._lock:
            return len(self._failures)

    def _prune(self, timestamps: Deque[float], now: float) -> None:
        cutoff = now - self.window_seconds
        while timestamps and timestamps[0] < cutoff:
            timestamps.popleft()

    def record_failure(self, key: str, now: Optional[float] = None) -> None:
        """Record one failed auth attempt from ``key`` (a source IP).

        A no-op while the guard is disabled (``threshold <= 0``) — there is
        nothing to count toward.
        """

        if self.threshold <= 0:
            return
        now = self.clock() if now is None else now
        with self._lock:
            timestamps = self._failures.get(key)
            if timestamps is None:
                if len(self._failures) >= self.max_tracked_sources:
                    oldest_key = next(iter(self._failures))
                    del self._failures[oldest_key]
                timestamps = collections.deque()
                self._failures[key] = timestamps
            self._prune(timestamps, now)
            timestamps.append(now)

    def is_locked_out(self, key: str, now: Optional[float] = None) -> Optional[float]:
        """Remaining lockout seconds for ``key``, or ``None`` if not locked out.

        Prunes stale (older than :attr:`window_seconds`) failures first, then
        answers based on the failures still within the window. Always
        ``None`` while the guard is disabled (``threshold <= 0``).
        """

        if self.threshold <= 0:
            return None
        now = self.clock() if now is None else now
        with self._lock:
            timestamps = self._failures.get(key)
            if timestamps is None:
                return None
            self._prune(timestamps, now)
            if not timestamps:
                del self._failures[key]
                return None
            if len(timestamps) < self.threshold:
                return None
            remaining = self.lockout_seconds - (now - timestamps[-1])
            return remaining if remaining > 0 else None

    def record_success(self, key: str) -> None:
        """Clear ``key``'s failure history entirely.

        A caller who mistypes a token a few times and then gets it right is
        never penalised: a subsequent wrong-token request afterwards starts
        as a fresh first failure.
        """

        with self._lock:
            self._failures.pop(key, None)
