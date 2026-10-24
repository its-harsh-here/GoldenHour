"""PHI redaction applied before any LLM call.

Names, phone numbers, 12-digit Aadhaar-like numbers, emails and ID numbers become tokens such as
[PATIENT_1]. The token map stays local; tokens are rehydrated only when final letters are rendered.
Domain-agnostic: packs supply the known entities and any extra ID patterns.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

BASE_PATTERNS: list[tuple[str, str]] = [
    ("EMAIL", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    ("AADHAAR", r"\b\d{4}[ -]?\d{4}[ -]?\d{4}\b"),
    ("PHONE", r"(?:\+91[ -]?)?\b[6-9]\d{4}[ -]?\d{5}\b"),
]


@dataclass
class Span:
    start: int
    end: int
    kind: str
    token: str
    text: str


@dataclass
class Redaction:
    text: str
    spans: list[Span]

    def as_dict(self) -> dict:
        return {"text": self.text, "spans": [asdict(s) for s in self.spans]}


def _norm(kind: str, value: str) -> str:
    if kind in ("PHONE", "AADHAAR"):
        return re.sub(r"\D", "", value)[-10:] if kind == "PHONE" else re.sub(r"\D", "", value)
    return value.strip().lower()


class Redactor:
    """Stateful per case, so the same person or number always maps to the same token."""

    def __init__(self, entities: list[tuple[str, str]], extra_patterns: list[tuple[str, str]] | None = None):
        self.mapping: dict[str, str] = {}
        self._tokens: dict[tuple[str, str], str] = {}
        self._counters: dict[str, int] = {}
        self._names: list[tuple[re.Pattern, str]] = []
        self._patterns = [(k, re.compile(p)) for k, p in (extra_patterns or []) + BASE_PATTERNS]
        for kind, value in entities:
            if value and value.strip():
                self._register_name(kind, value.strip())

    def _token_for(self, kind: str, value: str) -> str:
        key = (kind, _norm(kind, value))
        if key not in self._tokens:
            self._counters[kind] = self._counters.get(kind, 0) + 1
            token = f"[{kind}_{self._counters[kind]}]"
            self._tokens[key] = token
            self.mapping[token] = value
        return self._tokens[key]

    def _register_name(self, kind: str, name: str) -> None:
        token = self._token_for(kind, name)
        self._names.append((re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE), token))
        for part in name.split():
            if len(part) >= 3 and part[0].isupper() and not any(p.pattern == rf"\b{re.escape(part)}\b" for p, _ in self._names):
                self._names.append((re.compile(rf"\b{re.escape(part)}\b"), token))

    def redact(self, text: str) -> Redaction:
        found: list[Span] = []
        for pattern, token in self._names:
            for m in pattern.finditer(text):
                found.append(Span(m.start(), m.end(), token.strip("[]").rsplit("_", 1)[0], token, m.group()))
        for kind, pattern in self._patterns:
            for m in pattern.finditer(text):
                found.append(Span(m.start(), m.end(), kind, self._token_for(kind, m.group()), m.group()))
        # keep the longest span where they overlap, earliest first
        found.sort(key=lambda s: (s.start, -(s.end - s.start)))
        spans: list[Span] = []
        for span in found:
            if spans and span.start < spans[-1].end:
                continue
            spans.append(span)
        out, cursor = [], 0
        for span in spans:
            out.append(text[cursor : span.start])
            out.append(span.token)
            cursor = span.end
        out.append(text[cursor:])
        return Redaction("".join(out), spans)

    def redact_value(self, value):
        return self.redact(value).text if isinstance(value, str) else value


def rehydrate(text: str, mapping: dict[str, str]) -> str:
    """Swap tokens back to real values. Only called locally when rendering final documents."""
    for token, value in sorted(mapping.items(), key=lambda kv: -len(kv[0])):
        text = text.replace(token, value)
    return text


def contains_phi(text: str, mapping: dict[str, str]) -> bool:
    lowered = text.lower()
    return any(value.lower() in lowered for value in mapping.values() if len(value) >= 4)
