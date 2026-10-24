"""Live feed and day replay: a background thread that keeps work arriving at the desk.

Domain-agnostic. The pack supplies:
- `feed_arrival` (a new request lands in the inbox as NEW), `feed_desk` (simulated staff acting on pending
  generated cases) and `feed_prune` (retention) for the real-time feed;
- `day_plan` (arrivals across a window) and `day_event` (handle one simulated event, returning follow-ups)
  plus `day_clear` for a day replay.

Live arrivals are a Poisson process (exponential gaps around FEED_INTERVAL_S). Each one waits a few seconds
as NEW, then the agents pick it up. In live mode up to FEED_LIVE_PER_HOUR arrivals an hour run on the live
model; the rest run on the simulator (mock answers with model-like timing, `LLMClient.paced`), so every
arrival is visibly worked by the agents while a public deployment stays inside a free tier.
A day replay plays the last 24 hours on a simulated clock (`core.clock.at`), either as fast as the machine
allows (startup backfill) or paced over a few minutes for a demo. While a replay runs, live arrivals wait.
"""
from __future__ import annotations

import heapq
import random
import threading
import time
from contextlib import nullcontext

from core import clock

DAY_S = 24 * 3600
DAY_SOURCE = "day"  # the `source` a pack gives the cases a day replay creates


class DayReplay:
    """A simulated window of time: events (sim_ts, kind, ref) handled in time order."""

    def __init__(self, start: float, end: float, events: list[tuple[float, str, object]], speed: float | None):
        self.start, self.end, self.speed = start, end, speed
        self.heap: list[tuple[float, int, str, object]] = []
        self._n = 0
        for ts, kind, ref in events:
            self.push(ts, kind, ref)
        self.arrivals_total = sum(1 for e in events if e[1] == "arrive")
        self.arrivals_done = self.events_done = 0
        self.t0 = time.time()
        self.sim_now = start
        self.done = False
        self.error: str | None = None

    def push(self, ts: float, kind: str, ref: object) -> None:
        if ts <= self.end:
            heapq.heappush(self.heap, (ts, self._n, kind, ref))
            self._n += 1

    def target(self) -> float:
        if self.speed is None:
            return self.end
        return min(self.end, self.start + (time.time() - self.t0) * self.speed)

    def status(self) -> dict:
        span = self.end - self.start
        return {"active": not self.done, "mode": "backfill" if self.speed is None else "paced",
                "start": self.start, "end": self.end, "sim_now": self.sim_now,
                "progress": round(min(1.0, (self.sim_now - self.start) / span), 4) if span else 1.0,
                "arrivals_done": self.arrivals_done, "arrivals_total": self.arrivals_total,
                "events_done": self.events_done, "minutes": round(span / self.speed / 60, 1) if self.speed else None,
                "error": self.error}


class Feed:
    DESK_EVERY_S = 10
    DAY_TICK_S = 0.2
    PICKUP_S = (3.0, 8.0)  # how long a new arrival waits as NEW before the agents pick it up

    def __init__(self, runtime):
        s = runtime.settings
        self.rt = runtime
        self.interval_s = max(5.0, s.feed_interval_s)
        self.max_cases = s.feed_max_cases
        self.live_per_hour = max(0, s.feed_live_per_hour)
        self.simulate_desk = s.feed_simulate_desk
        self.busy = threading.RLock()  # held during a tick; a demo reset and a day replay take it too
        self.next_at: float | None = None
        self.last_arrival: str | None = None
        self.day: DayReplay | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_desk = 0.0
        self._last_progress = 0.0
        self._pending: list[tuple[float, str, bool]] = []  # (pick up at, case id, run on the live model)
        self._inflight: set[str] = set()  # arrivals the agents are working on right now
        self._live_times: list[float] = []

    # ---------------- live feed ----------------
    @property
    def enabled(self) -> bool:
        return bool(self.rt.store.kv_get("feed_enabled", True))

    def set_enabled(self, on: bool) -> None:
        self.rt.store.kv_set("feed_enabled", on)
        self.next_at = self._next_gap() if on else None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def day_active(self) -> bool:
        return self.day is not None and not self.day.done

    def case_clock(self, source: str | None) -> float | None:
        """While a day replay runs, the cases it plays live on the replay's clock (None: real time)."""
        day = self.day
        return day.sim_now if source == DAY_SOURCE and day is not None and not day.done else None

    def _next_gap(self) -> float:
        """Poisson arrivals: exponential gap around the mean, clamped to keep the demo lively."""
        gap = random.expovariate(1.0 / self.interval_s)
        return time.time() + min(max(gap, 5.0), self.interval_s * 4)

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self.next_at = time.time() + min(8.0, self.interval_s)  # first arrival shortly after boot
        self._thread = threading.Thread(target=self._loop, daemon=True, name="live-feed")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.DAY_TICK_S if self.day_active else 1.0):
            try:
                self.start_due()
            except Exception as err:
                print(f"live feed pickup failed: {type(err).__name__}: {err}", flush=True)
            if self.day_active:
                try:
                    self.day_step()
                except Exception as err:  # never let a replay kill the feed
                    print(f"day replay stopped: {type(err).__name__}: {err}", flush=True)
                    self._finish_day(f"{type(err).__name__}: {err}")
                continue
            if not self.enabled:
                self.next_at = None
                continue
            if self.next_at is None:
                self.next_at = self._next_gap()
            try:
                self.tick(arrive=time.time() >= self.next_at)
            except Exception as err:  # the feed must never die; log and carry on
                print(f"live feed tick failed: {type(err).__name__}: {err}", flush=True)
                self.next_at = self._next_gap()

    def tick(self, arrive: bool = True, desk_now: bool = False) -> dict:
        """One live-feed iteration (also called directly by tests)."""
        out: dict = {"arrived": None, "desk": []}
        with self.busy:
            now = time.time()
            if self.simulate_desk and (desk_now or now - self._last_desk >= self.DESK_EVERY_S):
                self._last_desk = now
                out["desk"] = self.rt.pack.feed_desk(self.rt, True, force=desk_now)
            if arrive:
                cid = out["arrived"] = self.rt.pack.feed_arrival(self.rt)
                self._pending.append((now + random.uniform(*self.PICKUP_S), cid, self._live_slot(now)))
                self.last_arrival = cid
                self.next_at = self._next_gap()
            self.rt.pack.feed_prune(self.rt, self.max_cases)
        return out

    def _live_slot(self, now: float) -> bool:
        """Whether this arrival runs on the live model: live mode, under the hourly cap, spread through the hour."""
        if not self.live_per_hour or self.rt.llm.mode != "live":
            return False
        self._live_times = [t for t in self._live_times if now - t < 3600]
        spacing = 3600 / self.live_per_hour / 2
        if len(self._live_times) >= self.live_per_hour or (self._live_times and now - self._live_times[-1] < spacing):
            return False
        self._live_times.append(now)
        return True

    def start_due(self, force: bool = False, wait: bool = False) -> list[str]:
        """Hand the arrivals whose wait is over to the agents (in the background unless `wait`)."""
        now = time.time()
        with self.busy:  # never hand a case to the agents while a reset or a new replay is clearing the desk
            due = [p for p in self._pending if force or p[0] <= now]
            self._pending = [p for p in self._pending if p not in due]
            self._inflight.update(cid for _, cid, _ in due)
        for _, cid, live in due:
            if wait:
                self._process(cid, live)
            else:
                threading.Thread(target=self._process, args=(cid, live), daemon=True, name=f"feed-{cid}").start()
        return [cid for _, cid, _ in due]

    def _process(self, case_id: str, live: bool) -> None:
        rt = self.rt
        try:
            if rt.cases.status(case_id) != "NEW" or rt.orchestrator.is_running(case_id):
                return  # reset, replaced by a replay, or someone pressed Process first
            rt.events.notify(case_id, "picked_up", {"model": "live" if live else "simulated"})
            with nullcontext() if live else rt.llm.paced():
                rt.orchestrator.run(case_id, force_mock=not live)
        finally:
            self._inflight.discard(case_id)

    def _wait_inflight(self, timeout_s: float = 15.0) -> None:
        stop_at = time.time() + timeout_s
        while self._inflight and time.time() < stop_at:
            time.sleep(0.1)

    # ---------------- day replay ----------------
    def start_day(self, minutes: float | None = None, end: float | None = None) -> dict:
        """Replay the 24 hours ending now: as fast as possible (minutes=None) or paced over `minutes`."""
        with self.busy:
            self._wait_inflight()  # let the agents finish the arrivals they are on before the desk is cleared
            self._pending.clear()
            end = end or time.time()
            start = end - DAY_S
            self.rt.pack.day_clear(self.rt)
            events = self.rt.pack.day_plan(self.rt, start, end, self.rt.settings.day_volume)
            self.day = DayReplay(start, end, events, None if not minutes else DAY_S / (minutes * 60))
            self.rt.events.notify(None, "day_started", self.day.status())
            return self.day.status()

    def day_step(self, budget_s: float = 0.6) -> int:
        """Handle the simulated events that are due, for at most `budget_s` of real time."""
        day = self.day
        if day is None or day.done:
            return 0
        handled = 0
        stop_at = time.perf_counter() + budget_s
        with self.busy:
            target = day.target()
            while day.heap and day.heap[0][0] <= target and time.perf_counter() < stop_at:
                ts, _, kind, ref = heapq.heappop(day.heap)
                day.sim_now = ts
                with clock.at(ts):
                    for follow in self.rt.pack.day_event(self.rt, kind, ref) or []:
                        day.push(*follow)
                day.events_done += 1
                day.arrivals_done += kind == "arrive"
                handled += 1
            if day.speed is not None:
                day.sim_now = max(day.sim_now, target)
            finished = target >= day.end and not (day.heap and day.heap[0][0] <= day.end)
        if finished:
            self._finish_day()
        elif time.time() - self._last_progress > 0.75:
            self._last_progress = time.time()
            self.rt.events.notify(None, "day_progress", day.status())
        return handled

    def run_day(self, minutes: float | None = None, end: float | None = None) -> dict:
        """Synchronous replay (tests, scripts)."""
        self.start_day(minutes, end)
        while self.day_active:
            self.day_step(budget_s=5.0)
        return self.day.status()

    def _finish_day(self, error: str | None = None) -> None:
        if self.day is None:
            return
        self.day.done = True
        self.day.error = error
        self.day.sim_now = self.day.end if not error else self.day.sim_now
        self.rt.events.notify(None, "day_done", self.day.status())
        self.next_at = self._next_gap()

    def status(self) -> dict:
        return {"enabled": self.enabled, "running": self.running, "interval_s": self.interval_s,
                "next_at": self.next_at if self.enabled and not self.day_active else None, "now": time.time(),
                "last_arrival": self.last_arrival, "arrivals": "poisson", "llm_mode": self.rt.llm.mode,
                "live_per_hour": self.live_per_hour if self.rt.llm.mode == "live" else 0,
                "live_last_hour": len([t for t in self._live_times if time.time() - t < 3600]),
                "waiting": len(self._pending), "simulate_desk": self.simulate_desk,
                "max_cases": self.max_cases, "day": self.day.status() if self.day else None}
