"""Live feed: a background thread that keeps work arriving at the desk.

Domain-agnostic. The pack supplies `feed_arrival` (a new request, processed on arrival),
`feed_desk` (simulated staff acting on pending feed cases) and `feed_prune` (retention).
By default feed cases run the deterministic mock pipeline so a public deployment never spends
LLM quota on its own; set FEED_LLM=inherit to use the server's LLM mode.
"""
from __future__ import annotations

import random
import threading
import time


class Feed:
    DESK_EVERY_S = 10

    def __init__(self, runtime):
        s = runtime.settings
        self.rt = runtime
        self.interval_s = max(5.0, s.feed_interval_s)
        self.max_cases = s.feed_max_cases
        self.force_mock = s.feed_llm != "inherit"
        self.simulate_desk = s.feed_simulate_desk
        self.busy = threading.Lock()  # held during a tick; a demo reset takes it too
        self.next_at: float | None = None
        self.last_arrival: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_desk = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.rt.store.kv_get("feed_enabled", True))

    def set_enabled(self, on: bool) -> None:
        self.rt.store.kv_set("feed_enabled", on)
        self.next_at = time.time() + self.interval_s if on else None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

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
        while not self._stop.wait(1.0):
            if not self.enabled:
                self.next_at = None
                continue
            if self.next_at is None:
                self.next_at = time.time() + self.interval_s
            try:
                self.tick(arrive=time.time() >= self.next_at)
            except Exception as err:  # the feed must never die; log and carry on
                print(f"live feed tick failed: {type(err).__name__}: {err}", flush=True)
                self.next_at = time.time() + self.interval_s

    def tick(self, arrive: bool = True, desk_now: bool = False) -> dict:
        """One feed iteration (also called directly by tests)."""
        out: dict = {"arrived": None, "desk": []}
        with self.busy:
            now = time.time()
            if self.simulate_desk and (desk_now or now - self._last_desk >= self.DESK_EVERY_S):
                self._last_desk = now
                out["desk"] = self.rt.pack.feed_desk(self.rt, self.force_mock, force=desk_now)
            if arrive:
                out["arrived"] = self.rt.pack.feed_arrival(self.rt, self.force_mock)
                self.last_arrival = out["arrived"]
                self.next_at = time.time() + self.interval_s * random.uniform(0.75, 1.25)
            self.rt.pack.feed_prune(self.rt, self.max_cases)
        return out

    def status(self) -> dict:
        return {"enabled": self.enabled, "running": self.running, "interval_s": self.interval_s,
                "next_at": self.next_at if self.enabled else None, "now": time.time(), "last_arrival": self.last_arrival,
                "llm": "mock" if self.force_mock else "inherit", "simulate_desk": self.simulate_desk, "max_cases": self.max_cases}
