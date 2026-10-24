"""Plain-Python state machine that runs a pack's agent pipeline (completed in M2)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from core.llm import LLMClient
from core.settings import Settings


@dataclass
class StepContext:
    """Everything one agent step may use. Agents return a dict merged into the case state."""

    case_id: str
    request: dict
    state: dict
    llm: LLMClient
    settings: Settings
    config: dict
    emit: Callable[..., None]
    force_mock: bool = False

    def model_label(self, model: str, source: str) -> str:
        return model if source in ("groq", "cache", "replay") else f"{source}:{model.split('/')[-1]}"
