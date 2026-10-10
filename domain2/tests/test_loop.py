from __future__ import annotations

import json
from dataclasses import replace

import pytest

from cwabench.validate import validate_run
from cwabench2 import config as config_mod
from cwabench2.runner import run

LOOP = """
[model]
mode = "llm"
cache = "{cache}"
seed_per_sample = true
[baselines]
summarizer = "llm"
window_turns = 4
[budgets]
input = [700]
[s6]
families = ["vt"]
arms = ["truncate-pinned", "window", "summary"]
tier = "700"
repeats = 2
"""


def test_s6_needs_seeds_that_fork_its_chains(small_config):
    config = small_config(extra=LOOP.format(cache="c").replace("seed_per_sample = true", ""))
    text = config.path.read_text().replace('suites = ["S0"]', 'suites = ["S0", "S6"]')
    config.path.write_text(text)
    with pytest.raises(config_mod.ConfigError, match="seed_per_sample"):
        config_mod.load(config.path)


def test_s6_puts_each_reply_in_the_next_payload_and_replays(small_config, tmp_path, monkeypatch):
    asked = []

    def chat(self, handed, overrides=None):  # a reply naming the chain's seed and a call count
        seed = (overrides or {}).get("seed", 7)
        asked.append((handed, seed))
        system = handed[0]["content"] if handed and handed[0]["role"] == "system" else ""
        if system.startswith("You keep a running summary"):
            text = "Summary: " + handed[-1]["content"].split("Assistant: ", 1)[-1][:60]
        else:
            text = f"Reply {seed}-{sum(1 for _, s in asked if s == seed)}"
        prompt = sum((len(m["content"].encode()) + 3) // 4 for m in handed)
        return {"text": text, "id": f"r{len(asked)}", "model": self.model, "finish_reason": "stop", "latency_ms": 1.0,
                "usage": {"prompt_tokens": prompt, "completion_tokens": 3, "total_tokens": prompt + 3},
                "cached_tokens": None}

    monkeypatch.setattr("cwabench.producers.llm_summarizer.Client.chat", chat)
    config = small_config(extra=LOOP.format(cache=tmp_path / "cache"))
    config.path.write_text(config.path.read_text().replace('suites = ["S0"]', 'suites = ["S0", "S6"]'))
    config = config_mod.load(config.path)
    first, status = run(config, build=False, log=lambda m: None)
    assert status == "pass" and validate_run(first) == []

    def rows(directory, name):
        return [json.loads(line) for line in (directory / name).read_text().splitlines()]

    turns, grades = rows(first, "suites/S6/turns.jsonl"), rows(first, "suites/S6/grades.jsonl")
    chains = {r["chain"] for r in turns}
    assert len(chains) == 2 * 3 * 2  # conversations × arms × samples
    assert all(r["outcome"] == "answered" for r in turns)
    by_chain = {}
    for r in turns:
        by_chain.setdefault(r["chain"], []).append(r)
    for chain, chain_turns in by_chain.items():
        assert [r["turn"] for r in chain_turns] == list(range(1, len(chain_turns) + 1))
        seed = 7 + int(chain.rsplit("#", 1)[1])
        assert all(r["reply"].startswith(f"Reply {seed}-") for r in chain_turns)  # each sample its own seed
    # Each reply is in the history of the next turn's payload: the window chains send native assistant messages
    replies = {r["reply"] for r in turns}
    histories = [m["content"] for handed, _ in asked for m in handed if m["role"] == "assistant"]
    assert histories and set(histories) <= replies
    # The summary chains summarize the model's replies, not the scripted ones
    summarized = [handed[-1]["content"] for handed, _ in asked
                  if handed and handed[0]["content"].startswith("You keep a running summary")]
    assert summarized and all("Assistant: Reply " in text for text in summarized)
    assert {g["arm"] for g in grades} == {"truncate-pinned", "window", "summary"}
    assert len(grades) == sum(len(c["probes"]) for c in _scripts(first)) * 3 * 2
    summary = json.loads((first / "suites/S6/summary.json").read_text())
    assert summary["model"]["chains"]["by_end"] == {"completed": 12}
    calls = len(asked)

    again, status = run(replace(config, model={**config.model, "mode": "replay", "concurrency": 1}), build=False,
                        log=lambda m: None)
    assert status == "pass" and len(asked) == calls  # the chains replay call by call, asking nothing

    def strip(directory, name):
        return [{k: v for k, v in row.items() if k not in ("run_id", "lookup_ms")} for row in rows(directory, name)]

    for name in ("suites/S6/turns.jsonl", "suites/S6/grades.jsonl"):
        assert strip(again, name) == strip(first, name)


def _scripts(run_dir):
    index = json.loads((run_dir / "conversations" / "vt" / "index.json").read_text())
    out = []
    for entry in index["conversations"]:
        digest = entry["script"].split(":", 1)[1]
        path = next(run_dir.glob(f"blobs/sha256/{digest[:2]}/{digest}.*"))
        out.append(json.loads(path.read_text()))
    return out
