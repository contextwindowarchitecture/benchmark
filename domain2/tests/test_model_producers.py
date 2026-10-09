from __future__ import annotations

import json

from cwabench2.application import producers
from cwabench2.config import MODEL_DEFAULTS
from cwabench2.conversations import generate
from cwabench2.conversations.values import carries, turn_values
from cwabench2.model import Model
from cwabench2.suites.s3_unreliability import percentile, spread


def test_turn_values_and_carries():
    script = generate("fr", 6, 20, 0, 10, {"tasks": ["record"], "fields": 4, "steps": 4, "filler_sentences": 1,
                                           "reply_sentences": 1})
    values = turn_values(script)
    first = next(iter(values.values()))
    text = " ".join(v for _, v in first)
    assert carries(f"So far: {text}.", first) and not carries("nothing", first)
    assert carries("budget 12,500", [("number", "12500")]) and not carries("12,550", [("number", "12500")])


class Scripted:
    """A fake endpoint for the producers: the extractor gets the figures so far, the summarizer a running digest."""

    def __init__(self):
        self.calls = 0

    def __call__(self, client, handed):
        self.calls += 1
        system, user = handed[0]["content"], handed[1]["content"]
        import re

        numbers = re.findall(r"\d[\d,]*", user)
        if system.startswith("You maintain the state"):
            text = "not json" if self.calls == 2 else json.dumps({f"k{i}": n for i, n in enumerate(numbers)})
        else:
            text = "Figures so far: " + ", ".join(numbers)
        return {"text": text, "id": f"r{self.calls}", "model": client.model, "finish_reason": "stop",
                "latency_ms": 1.0, "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                "cached_tokens": None}


def test_producers_run_turn_by_turn_cache_and_replay(tmp_path, monkeypatch):
    fake = Scripted()
    monkeypatch.setattr("cwabench.producers.llm_summarizer.Client.chat", lambda self, m: fake(self, m))
    script = generate("vt", 3, 15, 0, 10, {"variables": 2, "distractors": 1, "assignment_density": 0.5,
                                           "filler_sentences": 1, "reply_sentences": 1})
    model = Model(dict(MODEL_DEFAULTS), tmp_path, "llm")
    produced, rows, errors = producers.produce(model, "20261009T000000Z-0000000", [script], True, True, 10, 1,
                                               lambda m: None)
    out = produced[script["conversation_id"]]
    assert errors == [] and sorted(out.states) == list(range(16)) and sorted(out.summaries) == list(range(6))
    assert out.states[2] == out.states[1]  # the unparsable second reply keeps the state
    assert [r["parsed"] for r in rows if r["producer"] == "extractor"][1] is False
    calls = fake.calls
    replay = Model(dict(MODEL_DEFAULTS), tmp_path, "replay")
    again, _, errors = producers.produce(replay, "20261009T000000Z-0000000", [script], True, True, 10, 1,
                                         lambda m: None)
    assert errors == [] and fake.calls == calls and again[script["conversation_id"]] == out
    empty = Model({**MODEL_DEFAULTS, "cache": "other"}, tmp_path, "replay")
    _, _, errors = producers.produce(empty, "20261009T000000Z-0000000", [script], True, False, 10, 1, lambda m: None)
    assert errors and "CacheMiss" in errors[0]


def test_unreliability_percentiles():
    assert percentile([0, 100, 100, 100, 100], 10) == 40.0 and percentile([0, 100, 100, 100, 100], 90) == 100.0
    rows = [{"arm": "a", "tier": "8192", "conversation": "c", "probe_id": "p", "score": s} for s in (1, 1, 1, 1, 1)]
    rows += [{"arm": "a", "tier": "8192", "conversation": "c", "probe_id": "q", "score": s} for s in (0, 1, 0, 1, 0)]
    found = sorted(spread(rows)[("a", "8192")], key=lambda r: r[2])
    assert found[0][1:] == (100.0, 0.0) and found[1][1] == 100.0 and found[1][2] == 100.0
