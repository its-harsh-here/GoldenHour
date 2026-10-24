"""Prompt-injection screening for every ingested document: Prompt Guard 2 plus a regex fallback.

The model returns a malicious probability as a bare number. The regex layer supplies highlight
spans for the UI and keeps screening working in mock mode or when the guard model is unavailable.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from core.llm import LLMClient, LLMError

INJECTION_PATTERNS = [
    r"\bignore\s+(?:all|any|the|previous|prior|above|these)?\s*(?:rules|instructions|prompts|polic(?:y|ies)|guidelines)\b",
    r"(?:^|\n)\s*(?:SYSTEM|ASSISTANT|DEVELOPER)\s*:",
    r"\bdisregard\s+(?:all|any|the|previous|prior|above)\b",
    r"\byou\s+are\s+now\b",
    r"\bapprove\s+(?:the\s+)?(?:full|entire|whole)\s+amount\b",
    r"\boverride\s+(?:the\s+)?(?:rules|policy|decision|limits)\b",
    r"\b(?:jailbreak|do\s+anything\s+now|developer\s+mode)\b",
    r"\bact\s+as\s+(?:an?|the)\s+(?:admin|administrator|system|supervisor)\b",
    r"\bnew\s+instructions?\s*:",
]
_COMPILED = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]
THRESHOLD = 0.5
QUARANTINE_TOKEN = "[QUARANTINED: instruction-like text removed by the security screen]"


@dataclass
class GuardResult:
    detected: bool
    score: float
    method: str
    model: str | None = None
    sentences: list[dict] = field(default_factory=list)  # {start, end, text} spans in the screened text
    error: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)

    def quarantine(self, text: str) -> str:
        """Replace the offending sentences so the downstream LLM never reads them."""
        out, cursor = [], 0
        for s in sorted(self.sentences, key=lambda s: s["start"]):
            out.append(text[cursor : s["start"]])
            out.append(QUARANTINE_TOKEN)
            cursor = s["end"]
        out.append(text[cursor:])
        return "".join(out)


def _sentence_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    left = max(text.rfind("\n", 0, start), text.rfind(". ", 0, start) + 1 if text.rfind(". ", 0, start) >= 0 else -1)
    left = left + 1 if left >= 0 else 0
    nl, dot = text.find("\n", end), text.find(". ", end)
    candidates = [c for c in (nl, dot + 1 if dot >= 0 else -1) if c >= 0]
    right = min(candidates) if candidates else len(text)
    while left < right and text[left].isspace():
        left += 1
    return left, right


def find_injection_spans(text: str) -> list[dict]:
    """Sentence-level spans that match an injection pattern (merged, in order)."""
    spans: list[tuple[int, int]] = []
    for pattern in _COMPILED:
        for m in pattern.finditer(text):
            spans.append(_sentence_bounds(text, m.start(), m.end()))
    merged: list[list[int]] = []
    for s, e in sorted(set(spans)):
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [{"start": s, "end": e, "text": text[s:e]} for s, e in merged]


def _chunks(text: str, size: int = 700) -> list[str]:
    lines, chunks, cur = text.split("\n"), [], ""
    for line in lines:
        if len(cur) + len(line) + 1 > size and cur:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    return chunks or [text]


def parse_score(content: str | None) -> float:
    m = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", content or "")
    if not m:
        raise ValueError(f"no score in guard reply: {content!r}")
    return max(0.0, min(1.0, float(m.group())))


def screen(text: str, llm: LLMClient | None = None, model: str | None = None, force_mock: bool = False) -> GuardResult:
    """Screen already-redacted text. Detected if the model score >= 0.5 or a regex pattern matches."""
    sentences = find_injection_spans(text)
    regex_score = 0.99 if sentences else 0.01
    use_model = llm is not None and model and not force_mock and llm.mode != "mock"
    if not use_model:
        return GuardResult(bool(sentences), regex_score, "regex", None, sentences)
    try:
        scores = []
        for chunk in _chunks(text):
            comp = llm.complete(
                model=model,
                messages=[{"role": "user", "content": chunk}],
                mock=lambda _m, c=chunk: {"content": "0.99" if find_injection_spans(c) else "0.01"},
                max_tokens=8,
            )
            scores.append(parse_score(comp.content))
        score = max(scores)
    except (LLMError, ValueError) as err:
        return GuardResult(bool(sentences), regex_score, "regex (guard model unavailable)", model, sentences, str(err)[:200])
    detected = score >= THRESHOLD or bool(sentences)
    return GuardResult(detected, round(score, 4), "prompt_guard+regex", model, sentences)
