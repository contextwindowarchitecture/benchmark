from __future__ import annotations

import json

import pytest

from cwabench2 import baselines, stats
from cwabench2.application.snapshots import Point
from cwabench2.config import MODEL_DEFAULTS
from cwabench2.model import CacheMiss, Model, messages
from cwabench2.suites.s0_cases import BASELINE_SCRIPT

POINT = Point("probe", 3, BASELINE_SCRIPT["probes"][0])
HAND = baselines.Settings(window_turns=2, margin_percent=0)


class FakeEndpoint:
    """Stands in for Client.chat: answers with a fixed text and counts calls."""

    def __init__(self, text="3000"):
        self.text, self.calls = text, []

    def __call__(self, client, handed):
        self.calls.append(handed)
        return {"text": self.text, "id": f"r{len(self.calls)}", "model": client.model, "finish_reason": "stop",
                "latency_ms": 12.5, "usage": {"prompt_tokens": 40, "completion_tokens": 2, "total_tokens": 42},
                "cached_tokens": 0}


@pytest.fixture
def endpoint(monkeypatch):
    fake = FakeEndpoint()
    monkeypatch.setattr("cwabench.producers.llm_summarizer.Client.chat", lambda self, m, o=None: fake(self, m))
    return fake


def test_the_handoff_joins_system_entries_and_keeps_messages():
    payload = baselines.at("summary", BASELINE_SCRIPT, POINT, 40, HAND).payload
    handed = messages(payload)
    assert handed[0] == {"role": "system", "content": "Keep track.\n\nReply briefly.\n\nSummary of the earlier "
                                                      "conversation:\nAlpha is 1000."}
    assert [m["role"] for m in handed[1:]] == ["user", "assistant", "user", "assistant", "user"]
    truncated = baselines.at("truncate", BASELINE_SCRIPT, POINT, 12, HAND).payload
    assert messages(truncated)[0]["role"] == "assistant"  # no system message when the system prompt was dropped
    cwa = json.dumps({"system": [{"id": "i", "text": "S"}], "tools": [],
                      "messages": [{"role": "user", "content": "<history>x</history>"}]}).encode()
    assert messages(cwa) == [{"role": "system", "content": "S"}, {"role": "user", "content": "<history>x</history>"}]


def test_the_request_hash_covers_payload_model_and_parameters(tmp_path):
    payload = baselines.at("window", BASELINE_SCRIPT, POINT, 29, HAND).payload
    model = Model(dict(MODEL_DEFAULTS), tmp_path, "llm")
    first = model.request_sha256(payload)
    assert first == Model(dict(MODEL_DEFAULTS), tmp_path, "llm").request_sha256(payload)
    assert first != Model({**MODEL_DEFAULTS, "seed": 8}, tmp_path, "llm").request_sha256(payload)
    assert first != Model({**MODEL_DEFAULTS, "model": "other"}, tmp_path, "llm").request_sha256(payload)
    other = baselines.at("window", BASELINE_SCRIPT, POINT, 20, HAND).payload
    assert first != model.request_sha256(other)


def test_llm_fills_the_cache_and_replay_answers_from_it(tmp_path, endpoint):
    payload = baselines.at("window", BASELINE_SCRIPT, POINT, 29, HAND).payload
    llm = Model(dict(MODEL_DEFAULTS), tmp_path, "llm")
    first = llm.ask(payload, 0)
    assert not first.cache_hit and first.text == "3000" and len(endpoint.calls) == 1
    assert llm.ask(payload, 0).cache_hit and len(endpoint.calls) == 1
    assert not llm.ask(payload, 1).cache_hit and len(endpoint.calls) == 2  # another sample is another call
    replay = Model(dict(MODEL_DEFAULTS), tmp_path, "replay")
    again = replay.ask(payload, 0)
    assert again.cache_hit and again.text == first.text and again.provenance == first.provenance
    with pytest.raises(CacheMiss):
        replay.ask(payload, 2)
    assert len(endpoint.calls) == 2
    entry = json.loads(next((tmp_path / "model-cache").glob("*/*.json")).read_text())
    assert entry["format"] == "cwa-bench-d2/model-cache-entry/v1" and set(entry["material"]) == {
        "kind", "request_sha256", "sample"}


def test_the_bootstrap_resamples_conversations():
    clusters = {"a": [1.0, 1.0, 1.0, 1.0], "b": [0.0, 0.0], "c": [1.0, 0.0]}
    interval = stats.bootstrap(clusters, 500, 1)
    assert interval["low"] <= 5 / 8 <= interval["high"] and interval["method"] == "cluster-bootstrap/conversation"
    assert stats.bootstrap(clusters, 500, 1) == interval  # seeded
    assert stats.bootstrap({"a": [1.0, 1.0]}, 100, 1)["low"] == 1.0
    assert stats.bootstrap({}, 100, 1) is None


def test_each_sample_sends_its_own_seed_when_asked(tmp_path):
    payload = json.dumps({"system": [], "tools": [], "messages": [{"role": "user", "content": "hi"}]}).encode()
    plain = Model(dict(MODEL_DEFAULTS), tmp_path, "llm")
    seeded = Model({**MODEL_DEFAULTS, "seed_per_sample": True}, tmp_path, "llm")
    assert plain.request(payload, 3)["seed"] == 7 and plain.request_sha256(payload, 3) == plain.request_sha256(payload)
    assert seeded.request(payload, 0) == plain.request(payload, 0)  # sample 0 never changes
    assert seeded.request(payload, 3)["seed"] == 10
    assert seeded.request_sha256(payload, 3) != plain.request_sha256(payload)
