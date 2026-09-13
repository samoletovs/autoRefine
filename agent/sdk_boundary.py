"""Absolute caller deadlines around synchronous SDK/auth/response-body work.

Socket timeouts only measure inactivity. A daemon worker owns each client lease
until its call actually returns; the caller can abandon its result at a deadline
without closing or concurrently reusing that client. Only cleanup may subsequently
use a retired lease. Workers never execute model tools or publication code.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

log = logging.getLogger(__name__)
T = TypeVar("T")
_boundary_lock = threading.Lock()


class SdkDeadlineExceeded(TimeoutError):
    """The caller stopped waiting; a late SDK result is not usable."""


@dataclass
class _Call:
    invoke: Callable[[float], Any]
    deadline: float
    grace: float
    cleanup: bool
    late_result: Callable[[Any], None] | None
    done: threading.Event = field(default_factory=threading.Event)
    abandoned: bool = False
    value: Any = None
    error: BaseException | None = None


class SdkBoundary:
    """Serialize SDK operations and transfer results only within their deadline."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: deque[_Call] = deque()
        self._running = False
        self._retired = False

    @staticmethod
    def _late_cleanup(callback: Callable[[Any], None] | None, value: Any) -> None:
        if callback is None:
            return

        def cleanup() -> None:
            try:
                callback(value)
            except Exception:
                # There is no caller to receive an error from an abandoned result.
                log.exception("Deferred cleanup of a late SDK result failed")

        threading.Thread(target=cleanup, name="autorefine-late-cleanup", daemon=True).start()

    def call(
        self, invoke: Callable[[float], T], *, deadline: float,
        cleanup: bool = False, late_result: Callable[[T], None] | None = None,
    ) -> T:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SdkDeadlineExceeded("SDK call budget exhausted before invocation")
        request = _Call(invoke, deadline, remaining, cleanup, late_result)
        with self._lock:
            if self._retired and not cleanup:
                raise SdkDeadlineExceeded("SDK client lease retired after an abandoned call")
            self._calls.append(request)
            if not self._running:
                self._running = True
                threading.Thread(
                    target=self._drain, name="autorefine-sdk", daemon=True,
                ).start()

        request.done.wait(max(0.0, deadline - time.monotonic()))
        with self._lock:
            if (
                request.done.is_set() and not request.abandoned
                and time.monotonic() < deadline and (cleanup or not self._retired)
            ):
                if request.error is not None:
                    raise request.error
                return request.value
            # Atomically revoke the result handoff. A worker cannot deliver later
            # into a run that has already rolled back or returned to its caller.
            was_abandoned = request.abandoned
            request.abandoned = True
            self._retired = True
            completed_late = request.done.is_set() and not was_abandoned
        if completed_late and request.error is None:
            self._late_cleanup(request.late_result, request.value)
        raise SdkDeadlineExceeded(
            "SDK call exceeded its absolute caller deadline; client lease retired"
        )

    def _drain(self) -> None:
        while True:
            with self._lock:
                if not self._calls:
                    self._running = False
                    return
                request = self._calls.popleft()
                retired_work = self._retired and not request.cleanup
            value, error = None, None
            try:
                deadline = request.deadline
                if request.cleanup:
                    # If an earlier in-flight call held the lease past this
                    # caller's grace, cleanup is deferred, never concurrent.
                    deadline = max(deadline, time.monotonic() + request.grace)
                elif retired_work or request.abandoned or time.monotonic() >= deadline:
                    raise SdkDeadlineExceeded("Queued SDK call expired before execution")
                value = request.invoke(deadline)
            except BaseException as exc:
                error = exc
            with self._lock:
                late = (
                    request.abandoned or time.monotonic() >= request.deadline
                    or (self._retired and not request.cleanup)
                )
                request.abandoned = late
                request.value, request.error = value, error
                request.done.set()
                if late:
                    self._retired = True
            if late:
                if error is None:
                    self._late_cleanup(request.late_result, value)
                else:
                    log.warning("Abandoned SDK call finished with %s", type(error).__name__)


def client_boundary(client: Any) -> SdkBoundary:
    """One lease per client, including its run/thread/agent cleanup operations."""
    with _boundary_lock:
        boundary = vars(client).get("_autorefine_sdk_boundary")
        if not isinstance(boundary, SdkBoundary):
            boundary = SdkBoundary()
            setattr(client, "_autorefine_sdk_boundary", boundary)
        return boundary
