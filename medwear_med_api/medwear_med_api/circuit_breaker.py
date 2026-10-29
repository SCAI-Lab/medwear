"""
Simple thread-safe circuit breaker.

States:
  CLOSED    — normal; all calls pass through.
  OPEN      — too many failures; calls fail immediately without hitting the service.
  HALF_OPEN — cooldown elapsed; one probe call is allowed to test recovery.

Usage:
    _cb = CircuitBreaker(failure_threshold=5, recovery_timeout=60)

    try:
        result = _cb.call(some_sync_function, arg1, arg2)
    except CircuitOpenError:
        # service is known-down; handle gracefully
    except Exception:
        # real error from the function
"""

import logging
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)


class CircuitOpenError(RuntimeError):
    """Raised when a call is rejected because the circuit is OPEN."""


class CircuitBreaker:
    _CLOSED = "closed"
    _OPEN = "open"
    _HALF_OPEN = "half_open"

    def __init__(
        self,
        name: str = "default",
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout

        self._state = self._CLOSED
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def call(self, func: Callable, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            state = self._current_state()

        if state == self._OPEN:
            raise CircuitOpenError(
                f"Circuit '{self.name}' is OPEN — service unavailable. "
                f"Next probe in {self._seconds_until_probe():.0f}s."
            )

        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except CircuitOpenError:
            raise
        except Exception:
            self._on_failure()
            raise

    @property
    def state(self) -> str:
        with self._lock:
            return self._current_state()

    # ------------------------------------------------------------------
    # Internal helpers (must be called with _lock held where noted)
    # ------------------------------------------------------------------

    def _current_state(self) -> str:
        if self._state == self._OPEN:
            if self._opened_at is not None and (
                time.monotonic() - self._opened_at >= self.recovery_timeout
            ):
                self._state = self._HALF_OPEN
                logger.info(f"Circuit '{self.name}' → HALF_OPEN (probing)")
        return self._state

    def _on_success(self) -> None:
        with self._lock:
            if self._state in (self._HALF_OPEN, self._OPEN):
                logger.info(f"Circuit '{self.name}' → CLOSED (recovered)")
            self._failures = 0
            self._opened_at = None
            self._state = self._CLOSED

    def _on_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._state == self._HALF_OPEN or self._failures >= self.failure_threshold:
                self._state = self._OPEN
                self._opened_at = time.monotonic()
                logger.warning(
                    f"Circuit '{self.name}' → OPEN "
                    f"(failures={self._failures}, threshold={self.failure_threshold})"
                )

    def _seconds_until_probe(self) -> float:
        if self._opened_at is None:
            return 0.0
        elapsed = time.monotonic() - self._opened_at
        return max(0.0, self.recovery_timeout - elapsed)
