"""Runtime configuration loaded from `.env` (python-dotenv) and the process environment."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=False)

VALID_MODES = ("live", "mock", "replay")


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    llm_mode: str
    llm_mode_note: str
    host: str
    port: int
    llm_api_key: str
    llm_base_url: str
    model_fast: str
    model_reason: str
    model_guard: str
    llm_rpm: int
    llm_tpm: int
    llm_timeout_s: float
    reasoning_effort: str
    db_path: Path
    pack: str
    replay_record: bool
    seed_history: bool
    live_feed: bool
    feed_interval_s: float
    feed_max_cases: int
    feed_llm: str
    feed_simulate_desk: bool
    day_volume: int
    day_backfill: bool
    day_replay_minutes: float

    def model_id(self, role: str) -> str:
        """The provider's model id behind a logical role (fast / reason / guard)."""
        return {"fast": self.model_fast, "reason": self.model_reason, "guard": self.model_guard}[role]


def get_settings() -> Settings:
    """Read settings fresh each call so tests and the admin console can change env at runtime.

    `live` needs an OpenAI-compatible endpoint (LLM_BASE_URL) and the three model ids; without them the
    desk serves the recorded runs (`replay`) instead of failing every call. An unknown mode does the same.
    """
    requested = os.getenv("LLM_MODE", "mock").strip().lower()
    base_url = os.getenv("LLM_BASE_URL", "").strip()
    models = {k: os.getenv(k, "").strip() for k in ("MODEL_FAST", "MODEL_REASON", "MODEL_GUARD")}
    mode, note = requested, ""
    if requested not in VALID_MODES:
        mode, note = "replay", f"unknown LLM_MODE '{requested}': serving recorded runs (replay)"
    elif requested == "live" and not (base_url and all(models.values())):
        mode, note = "replay", "LLM_MODE=live needs LLM_BASE_URL, MODEL_FAST, MODEL_REASON and MODEL_GUARD: serving recorded runs (replay) until they are set"
    db = Path(os.getenv("DB_PATH", str(ROOT / "data" / "goldenhour.db")))
    return Settings(
        llm_mode=mode,
        llm_mode_note=note,
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
        llm_base_url=base_url,
        model_fast=models["MODEL_FAST"],
        model_reason=models["MODEL_REASON"],
        model_guard=models["MODEL_GUARD"],
        llm_rpm=int(os.getenv("LLM_RPM", "30")),
        llm_tpm=int(os.getenv("LLM_TPM", "8000")),
        llm_timeout_s=float(os.getenv("LLM_TIMEOUT_S", "30")),
        reasoning_effort=os.getenv("LLM_REASONING_EFFORT", "low").strip().lower(),
        db_path=db,
        pack=os.getenv("PACK", "health_preauth"),
        replay_record=_bool(os.getenv("REPLAY_RECORD"), False),
        seed_history=_bool(os.getenv("SEED_HISTORY"), True),
        live_feed=_bool(os.getenv("LIVE_FEED"), True),
        feed_interval_s=float(os.getenv("FEED_INTERVAL_S", "40")),
        feed_max_cases=int(os.getenv("FEED_MAX_CASES", "40")),
        feed_llm=os.getenv("FEED_LLM", "mock").strip().lower(),
        feed_simulate_desk=_bool(os.getenv("FEED_SIMULATE_DESK"), True),
        day_volume=int(os.getenv("DAY_VOLUME", "200")),
        day_backfill=_bool(os.getenv("DAY_BACKFILL"), True),
        day_replay_minutes=float(os.getenv("DAY_REPLAY_MINUTES", "3")),
    )
