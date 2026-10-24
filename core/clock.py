"""Business clock: real time, or a simulated timestamp while a day replay processes an event.

Everything that records *when something happened on the desk* (case received/updated/decided, SLA pauses,
trace events, audit blocks, notifications) reads `now()`. A day replay wraps each simulated event in
`at(ts)`, so a whole day can be played in minutes with every timestamp on the day's own timeline.
The context variable is per thread, so API requests keep seeing real time while a replay runs.
"""
from __future__ import annotations

import contextvars
import time
from contextlib import contextmanager
from typing import Iterator

_SIM_TS: contextvars.ContextVar[float | None] = contextvars.ContextVar("sim_ts", default=None)


def now() -> float:
    ts = _SIM_TS.get()
    return time.time() if ts is None else ts


def simulated() -> bool:
    return _SIM_TS.get() is not None


def advance(seconds: float) -> None:
    """Inside `at()`, move simulated time forward, as if the step just done had taken `seconds`."""
    ts = _SIM_TS.get()
    if ts is not None:
        _SIM_TS.set(ts + seconds)


@contextmanager
def at(ts: float) -> Iterator[None]:
    token = _SIM_TS.set(ts)
    try:
        yield
    finally:
        _SIM_TS.reset(token)
