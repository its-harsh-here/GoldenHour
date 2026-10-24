"""Runtime configuration loaded from `.env` (python-dotenv) and the process environment."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=False)

VALID_MODES = ("groq", "mock", "replay")


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    llm_mode: str
    host: str
    port: int
    groq_api_key: str
    groq_base_url: str
    model_fast: str
    model_reason: str
    model_guard: str
    groq_rpm: int
    groq_tpm: int
    llm_timeout_s: float
    db_path: Path
    pack: str
    replay_record: bool
    seed_history: bool


def get_settings() -> Settings:
    """Read settings fresh each call so tests and the admin console can change env at runtime."""
    mode = os.getenv("LLM_MODE", "mock").strip().lower()
    if mode not in VALID_MODES:
        mode = "mock"
    db = Path(os.getenv("DB_PATH", str(ROOT / "data" / "goldenhour.db")))
    return Settings(
        llm_mode=mode,
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        groq_api_key=os.getenv("GROQ_API_KEY", "").strip(),
        groq_base_url=os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
        model_fast=os.getenv("MODEL_FAST", "openai/gpt-oss-20b"),
        model_reason=os.getenv("MODEL_REASON", "openai/gpt-oss-120b"),
        model_guard=os.getenv("MODEL_GUARD", "meta-llama/llama-prompt-guard-2-86m"),
        groq_rpm=int(os.getenv("GROQ_RPM", "30")),
        groq_tpm=int(os.getenv("GROQ_TPM", "8000")),
        llm_timeout_s=float(os.getenv("LLM_TIMEOUT_S", "30")),
        db_path=db,
        pack=os.getenv("PACK", "health_preauth"),
        replay_record=_bool(os.getenv("REPLAY_RECORD"), False),
        seed_history=_bool(os.getenv("SEED_HISTORY"), True),
    )
