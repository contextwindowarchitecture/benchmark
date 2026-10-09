"""The optional LLM summarizer: an OpenAI-compatible chat client behind the variant cache (domain-1-plan.md, 9.1).

`base_url` reaches any server that speaks `POST /chat/completions` (OpenAI, OpenRouter, vLLM, SGLang, Ollama, a local
MLX server). Requests ask for temperature 0 and, when configured, a seed; `extra_body` passes server-specific fields
such as `chat_template_kwargs`. Every request parameter goes into the cache key and into the variant's method string
(`llm-summarize/v1 <model>@<params sha8>`), so a trace names exactly how its variant was made.

Each chunk is summarized `repeat_k` times as samples 0 … k−1, each its own cache key and so its own model call: sample
0 becomes the frozen variant, and all k measure repeat stability. In `llm` mode a miss calls the endpoint and fills
the cache; in `replay` mode a miss raises CacheMiss. The model is never called during assembly: only this module calls
it, and only before freeze.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .cache import Cache, CacheMiss, key, sha256

PROMPT_ID = "summarize/v1"
SYSTEM = ("You shorten passages for a retrieval system. Write a summary that uses only facts stated in the passage. "
          "Keep names, numbers, dates, ids and URLs exactly as written. Add no advice, instructions, headings or "
          "commentary. Output only the summary text.")
USER = "Summarize the passage below in at most {target} words.\n\nPassage:\n{body}"
JUDGE_ID = "judge/v1"
JUDGE_SYSTEM = ("You check summaries against their source. Answer SUPPORTED if every statement in the summary is "
                "stated in the source, otherwise UNSUPPORTED. Answer with that one word.")
JUDGE_USER = "Source:\n{parent}\n\nSummary:\n{variant}"
_THINK = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL)


class EndpointError(Exception):
    pass


@dataclass
class Result:
    text: str
    cache_hit: bool
    key: str
    provenance: dict
    lookup_ms: float  # time to answer: the model call on a miss, the cache read on a hit


def prompt_sha256(system: str, user: str) -> str:
    return sha256(system + "\0" + user)


class Client:
    def __init__(self, settings: dict):
        self.base_url = str(settings.get("base_url", "http://127.0.0.1:8000/v1")).rstrip("/")
        self.model = str(settings.get("model", ""))
        self.timeout_s = float(settings.get("request_timeout_s", 120))
        self.retries = int(settings.get("retries", 2))
        env = settings.get("api_key_env", "CWA_BENCH_SUMMARIZER_KEY")
        self._key = os.environ.get(env, "") if env else ""
        self.params = {"temperature": settings.get("temperature", 0),
                       "max_tokens": int(settings.get("max_tokens", 400))}
        if settings.get("seed") is not None:
            self.params["seed"] = int(settings["seed"])
        self.extra_body = dict(settings.get("extra_body", {}))

    @property
    def host(self) -> str:
        return urllib.parse.urlsplit(self.base_url).netloc

    def request_params(self) -> dict:
        """Everything about a request except its messages; part of every cache key."""
        return {**self.params, "extra_body": self.extra_body}

    def complete(self, system: str, user: str) -> dict:
        return self.chat([{"role": "system", "content": system}, {"role": "user", "content": user}])

    def body(self, messages: list[dict]) -> dict:
        """The request body for `messages`: the model, the messages, the parameters and the server's extra fields."""
        return {"model": self.model, "messages": messages, **self.params, **self.extra_body}

    def chat(self, messages: list[dict]) -> dict:
        """One chat completion. `usage` keeps the three standard counts; `cached_tokens` is the prompt prefix the
        server reports it reused, or None when it reports none."""
        headers = {"Content-Type": "application/json"}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        request = urllib.request.Request(f"{self.base_url}/chat/completions",
                                         json.dumps(self.body(messages)).encode("utf-8"), headers, method="POST")
        last = None
        for attempt in range(self.retries + 1):
            started = time.perf_counter()
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                    data = json.loads(response.read().decode("utf-8"))
                latency = (time.perf_counter() - started) * 1000
                choice = data["choices"][0]
                text = choice["message"].get("content") or ""
                usage = data.get("usage") or {}
                details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
                cached = details.get("cached_tokens")
                return {"text": text, "id": data.get("id"), "model": data.get("model"),
                        "finish_reason": choice.get("finish_reason"), "latency_ms": round(latency, 3),
                        "usage": {k: usage[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                                  if isinstance(usage.get(k), int)},
                        "cached_tokens": cached if isinstance(cached, int) and not isinstance(cached, bool) else None}
            except urllib.error.HTTPError as error:
                last = f"HTTP {error.code}: {error.read()[:300]!r}"
                if error.code < 500 and error.code != 429:
                    break
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, IndexError) as error:
                last = f"{type(error).__name__}: {error}"
            time.sleep(min(8, 2 ** attempt))
        raise EndpointError(f"{self.base_url}: {last}")


class Summarizer:
    def __init__(self, settings: dict, cache: Cache, mode: str):
        if mode not in ("llm", "replay"):
            raise ValueError(f"the LLM summarizer runs in llm or replay mode, not {mode}")
        self.mode = mode
        self.cache = cache
        self.client = Client(settings)
        self.ratio = float(settings.get("target_ratio", 0.35))
        self.min_words = int(settings.get("min_target_words", 8))
        self.params = self.client.request_params()
        self.params_sha256 = sha256(json.dumps(self.params, sort_keys=True))
        self.prompt_sha256 = prompt_sha256(SYSTEM, USER)
        self.method = f"llm-summarize/v1 {self.client.model}@{self.params_sha256[:8]}"

    def target(self, parent_tokens: int) -> int:
        return max(self.min_words, round(parent_tokens * self.ratio))

    def material(self, body: str, sample: int) -> dict:
        return {"kind": "summarize", "parent_sha256": sha256(body), "prompt_id": PROMPT_ID,
                "prompt_sha256": self.prompt_sha256, "model": self.client.model, "params": self.params,
                "target_ratio": self.ratio, "min_target_words": self.min_words, "sample": sample}

    def _answer(self, material: dict, system: str, user: str, sample: int, parent_sha: str) -> Result:
        started = time.perf_counter()
        found = self.cache.get(material)
        if found is not None:
            return Result(found["text"], True, found["key"], found["provenance"],
                          round((time.perf_counter() - started) * 1000, 3))
        if self.mode == "replay":
            raise CacheMiss(f"no cached {material['kind']} for parent {parent_sha[:12]}, sample {sample}")
        response = self.client.complete(system, user)
        text = response["text"]
        stripped = bool(_THINK.match(text))
        text = _THINK.sub("", text).strip()
        provenance = {
            "model": self.client.model, "response_model": response["model"], "endpoint_host": self.client.host,
            "params": self.params, "params_sha256": self.params_sha256, "prompt_id": material["prompt_id"],
            "prompt_sha256": material["prompt_sha256"], "response_id": response["id"], "usage": response["usage"],
            "latency_ms": response["latency_ms"], "finish_reason": response["finish_reason"],
            "stripped_reasoning": stripped, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "sample": sample, "parent_sha256": parent_sha,
        }
        entry = self.cache.put(material, text, provenance)
        return Result(entry["text"], False, entry["key"], entry["provenance"], response["latency_ms"])

    def summarize(self, body: str, parent_tokens: int, sample: int) -> Result:
        material = self.material(body, sample)
        return self._answer(material, SYSTEM, USER.format(target=self.target(parent_tokens), body=body), sample,
                            material["parent_sha256"])

    def judge(self, parent: str, variant: str) -> Result:
        """The optional LLM-as-judge faithfulness check: reported, never gating (9.3)."""
        material = {"kind": "judge", "parent_sha256": sha256(parent), "variant_sha256": sha256(variant),
                    "prompt_id": JUDGE_ID, "prompt_sha256": prompt_sha256(JUDGE_SYSTEM, JUDGE_USER),
                    "model": self.client.model, "params": self.params, "sample": 0}
        return self._answer(material, JUDGE_SYSTEM, JUDGE_USER.format(parent=parent, variant=variant), 0,
                            material["parent_sha256"])

    def would_offer(self, body: str, sample: int = 0) -> bool:
        """Whether the cache holds a variant for this exact body: after an edit, it must not."""
        return self.cache.peek(self.material(body, sample))


__all__ = ["CacheMiss", "Client", "EndpointError", "Result", "Summarizer", "key"]
