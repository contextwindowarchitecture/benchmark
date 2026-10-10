"""The model: the request canon, the endpoint client, the cache and replay (domain-2-plan.md, 9).

**The handoff.** Every arm's payload has one shape (`cwa-messages/v1`'s request: system entries, tools, messages),
and every payload reaches the model the same way. The system entries' texts, joined by a blank line, become one
`system` message when there are any. The payload's messages follow, unchanged. Nothing is added: the text the model
reads is the text the payload's count describes (SPEC.md, section 1, a conformant application's handoff).

**The request** is the client's body for those messages: the model, the messages, the parameters (temperature, seed,
max_tokens) and the server's extra fields. Its SHA-256 over RFC 8785 is `request_sha256`, so a change to the payload,
the model or any parameter is another request.

**The cache** is Domain 1's content-addressed cache (cwabench.producers.cache) under `[model].cache`, with entries of
kind `cwa-bench-d2/model-cache-entry/v1`. A key's material is the request's hash and the sample index, not the request
itself, so the committed cache stays small; each entry keeps the reply and its provenance. Samples 0 … K−1 of one
payload are separate keys and separate calls, since a server can vary at temperature 0.

**Samples and seeds.** With `[model].seed_per_sample`, sample k is sent with the configured seed plus k, so the samples
of a sampled suite (S3, S6) are independent draws, each reproducible; without it every sample sends the same seed,
and on a server that honours seeds the samples differ only by the server's own nondeterminism. Sample 0 always sends
the configured seed, so turning it on changes no request of the producers or of a suite's first sample. The omlx
pilots ran without it; domain2.toml sets it.

**Modes.** `llm` answers from the cache and calls the endpoint on a miss, filling the cache. `replay` answers from the
cache alone, and a miss is an error. Nothing else in the harness calls a model.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from cwabench.canon import jcs
from cwabench.producers.cache import Cache, CacheMiss
from cwabench.producers.llm_summarizer import Client, EndpointError

FORMAT = "cwa-bench-d2/model-cache-entry/v1"
MODES = ("llm", "replay")

__all__ = ["CacheMiss", "EndpointError", "Model", "Reply", "messages"]


def messages(payload: bytes) -> list[dict]:
    """The chat messages a payload is handed to the model as."""
    request = json.loads(payload.decode("utf-8"))
    system = "\n\n".join(entry["text"] for entry in request.get("system", []))
    handed = [{"role": "system", "content": system}] if system else []
    for message in request.get("messages", []):
        content = message["content"]
        if not isinstance(content, str):  # cwa-message-blocks/v1: content parts, joined in order
            content = "".join(block["text"] for block in content)
        handed.append({"role": message["role"], "content": content})
    return handed


@dataclass(frozen=True)
class Reply:
    text: str
    key: str
    request_sha256: str
    cache_hit: bool
    lookup_ms: float
    provenance: dict


class Model:
    def __init__(self, settings: dict, root: Path, mode: str):
        if mode not in MODES:
            raise ValueError(f"the model runs in {' or '.join(MODES)} mode, not {mode}")
        self.mode = mode
        self.settings = settings
        self.client = Client(settings)
        self.cache = Cache(root / settings.get("cache", "model-cache"), writable=mode == "llm", format=FORMAT)
        self.calls = 0
        self._lock = threading.Lock()

    @property
    def params(self) -> dict:
        return self.client.request_params()

    def overrides(self, sample: int) -> dict:
        """What a sample's request changes of the configured parameters: its own seed, with `seed_per_sample`."""
        seed = self.client.params.get("seed")
        if self.settings.get("seed_per_sample") and seed is not None and sample:
            return {"seed": seed + sample}
        return {}

    def request(self, payload: bytes, sample: int = 0) -> dict:
        return self.client.body(messages(payload), self.overrides(sample))

    def request_sha256(self, payload: bytes, sample: int = 0) -> str:
        return hashlib.sha256(jcs.serialize_bytes(self.request(payload, sample))).hexdigest()

    def material(self, payload: bytes, sample: int) -> dict:
        return {"kind": "chat", "request_sha256": self.request_sha256(payload, sample), "sample": sample}

    def ask(self, payload: bytes, sample: int) -> Reply:
        material = self.material(payload, sample)
        started = time.perf_counter()
        found = self.cache.get(material)
        if found is not None:
            return Reply(found["text"], found["key"], material["request_sha256"], True,
                         round((time.perf_counter() - started) * 1000, 3), found["provenance"])
        if self.mode == "replay":
            raise CacheMiss(f"no cached reply for request {material['request_sha256'][:12]}, sample {sample}")
        response = self.client.chat(messages(payload), self.overrides(sample))
        with self._lock:
            self.calls += 1
        provenance = {
            "model": self.client.model, "response_model": response["model"], "endpoint_host": self.client.host,
            "params": {**self.params, **self.overrides(sample)}, "response_id": response["id"],
            "finish_reason": response["finish_reason"],
            "usage": response["usage"], "cached_tokens": response["cached_tokens"],
            "latency_ms": response["latency_ms"], "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "sample": sample, "payload_sha256": hashlib.sha256(payload).hexdigest(),
        }
        entry = self.cache.put(material, response["text"], provenance)
        return Reply(entry["text"], entry["key"], material["request_sha256"], False, response["latency_ms"],
                     entry["provenance"])

    def server(self) -> dict | None:
        """What the endpoint lists under the configured model name in /models (a vLLM server adds the weights it
        loaded as `root`, and `max_model_len`), so a run records which server answered. Asked in llm mode only, since
        a replay reaches no server; None when the endpoint lists nothing under the name or cannot be reached."""
        import urllib.request

        if self.mode != "llm":
            return None
        try:
            request = urllib.request.Request(f"{self.client.base_url}/models", headers=self.client.headers())
            with urllib.request.urlopen(request, timeout=10) as response:
                listed = json.loads(response.read().decode("utf-8")).get("data", [])
        except (OSError, ValueError, AttributeError):
            return None
        return next((entry for entry in listed if isinstance(entry, dict) and entry.get("id") == self.client.model),
                    None)
