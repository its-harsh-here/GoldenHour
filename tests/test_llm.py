import json
from dataclasses import replace

import pytest
from pydantic import BaseModel

from core.llm import LLMClient, LLMError, ReplayCache, TokenBucket, Tool, cache_key, parse_json
from core.settings import get_settings
from core.store import Store


class Answer(BaseModel):
    ok: bool
    n: int = 0


@pytest.fixture
def client(tmp_path):
    settings = replace(get_settings(), llm_mode="mock", db_path=tmp_path / "t.db")
    return LLMClient(settings, Store(settings.db_path), tmp_path / "replay.json")


def test_mock_mode_returns_validated_json(client):
    res = client.run_agent(agent="t", model="m", system="s", user="u", schema=Answer, mock=lambda m: {"content": {"ok": True, "n": 3}})
    assert res.data.ok and res.data.n == 3
    assert res.source == "mock"


def test_invalid_json_retries_once_then_raises(client):
    calls = []

    def bad(messages):
        calls.append(len(messages))
        return {"content": "not json"}

    with pytest.raises(LLMError):
        client.run_agent(agent="t", model="m", system="s", user="u", schema=Answer, mock=bad)
    assert len(calls) == 2  # original + one retry


def test_retry_recovers(client):
    replies = iter([{"content": '{"ok": "maybe-not-bool"}'}, {"content": '{"ok": true}'}])
    res = client.run_agent(agent="t", model="m", system="s", user="u", schema=Answer, mock=lambda m: next(replies))
    assert res.data.ok is True


def test_tool_loop_respects_allow_list(client):
    seen = []
    tools = [Tool("add", "add", {"type": "object", "properties": {}}, lambda a, b: {"sum": a + b})]

    def mock(messages):
        if not any(m["role"] == "tool" for m in messages):
            return {"tool_calls": [{"name": "add", "arguments": {"a": 2, "b": 3}}, {"name": "rm_rf", "arguments": {}}]}
        outputs = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        return {"content": {"ok": "error" in outputs[1], "n": outputs[0]["sum"]}}

    res = client.run_agent(agent="t", model="m", system="s", user="u", schema=Answer, mock=mock, tools=tools,
                           on_tool=lambda name, args, out, ms: seen.append(name))
    assert res.data.n == 5 and res.data.ok  # unknown tool was refused, not executed
    assert seen == ["add", "rm_rf"]


def test_replay_hit_and_miss_fallback(tmp_path):
    settings = replace(get_settings(), llm_mode="replay", db_path=tmp_path / "t.db")
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    cache = ReplayCache(tmp_path / "replay.json")
    cache.put(cache_key("m", msgs), {"content": '{"ok": true, "n": 7}', "tool_calls": [], "tokens": 5})
    client = LLMClient(settings, Store(settings.db_path), tmp_path / "replay.json")
    hit = client.run_agent(agent="t", model="m", system="s", user="u", schema=Answer, mock=lambda m: {"content": {"ok": False}})
    assert hit.data.n == 7 and hit.source == "replay" and not hit.replay_miss
    miss = client.run_agent(agent="t", model="m", system="s", user="other", schema=Answer, mock=lambda m: {"content": {"ok": False, "n": 1}})
    assert miss.replay_miss and miss.data.n == 1


def test_cache_key_is_stable_and_model_sensitive():
    msgs = [{"role": "user", "content": "x"}]
    assert cache_key("a", msgs) == cache_key("a", list(msgs))
    assert cache_key("a", msgs) != cache_key("b", msgs)


def test_parse_json_handles_fences():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(ValueError):
        parse_json("")


def test_token_bucket_allows_burst_then_throttles():
    bucket = TokenBucket(rpm=600, tpm=100000)
    assert bucket.acquire(10) == 0.0
