"""Do forged chat-template markers become special tokens? (domain-5-plan.md, 8, S1; 15)

The `marker` carrier writes a template's markers (`<|im_end|>`, `<|im_start|>system`) into the content as text.
Whether the model then reads a real turn boundary is the server's doing: its chat template renders the messages and
its tokenizer may read marker text in a message as the special token it spells. This check asks the served model's
tokenizer (vLLM's `/tokenize`, with the messages the handoff gives and the generation prompt added) how many of each
marker's special token every arm's payload holds, for the first `[s1].tokenize_cells` marker cells of each task
family, against the same instance's clean payload in the same arm. A payload with more is one where the forged
markers became real ones. CWA's escaping writes `<` and `>` as entities in every body, so no marker survives as text
there; the baselines pass the content through.

Each reply is cached under `tokenize-cache/` by the request's SHA-256 over RFC 8785, holding only the counts, and
committed like the model cache. `[s1].tokenize = "server"` asks the server on a miss and fills the cache; `"replay"`
reads the cache alone, a miss leaving the row unread; `"off"` skips the check. No text is generated.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.request
from collections import defaultdict

from cwabench.canon import jcs

from .. import baselines, grid, output
from ..application.snapshot import freeze
from ..arms import ARMS
from ..attacks.carriers import MARKERS
from ..handoff import messages


class TokenizeError(Exception):
    pass


def _post(base_url: str, body: dict, key: str | None) -> dict:
    headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})}
    request = urllib.request.Request(base_url.rstrip("/").removesuffix("/v1") + "/tokenize",
                                     data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except OSError as error:
        raise TokenizeError(str(error)) from None


class Tokenizer:
    def __init__(self, config):
        self.mode, self.model = config.tokenize, config.tokenize_model
        self.base_url = config.tokenize_base_url
        self.key = os.environ.get("CWA_BENCH_MODEL_KEY")
        self.cache = config.root / "tokenize-cache"

    def _cached(self, body: dict, read) -> dict | None:
        digest = hashlib.sha256(jcs.serialize_bytes(body)).hexdigest()
        path = self.cache / digest[:2] / f"{digest}.json"
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
        if self.mode != "server":
            return None
        entry = {"request_sha256": digest, "model": self.model, **read(_post(self.base_url, body, self.key))}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entry, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        return entry

    def marker_ids(self, template: str) -> dict[str, int] | None:
        found = {}
        for marker in sorted({m for side in MARKERS[template] for m in re.findall(r"<\|[a-z_]+\|>", side)}):
            body = {"model": self.model, "prompt": marker, "add_special_tokens": False}
            entry = self._cached(body, lambda reply: {"tokens": reply["tokens"]})
            if entry is None:
                return None
            if len(entry["tokens"]) == 1:
                found[marker] = entry["tokens"][0]
        return found

    def specials(self, payload: bytes, ids: dict[str, int]) -> dict[str, int] | None:
        body = {"model": self.model, "messages": messages(payload), "add_generation_prompt": True}

        def read(reply):
            tokens = reply["tokens"]
            return {"count": len(tokens), "special": {m: tokens.count(i) for m, i in sorted(ids.items())}}

        entry = self._cached(body, read)
        return None if entry is None else entry["special"]


def _payload(ctx, task: dict, items: list[dict], arm: str) -> bytes:
    if ARMS[arm].kind == "cwa":
        return freeze(ctx.contract, task, items, arm, ctx.config.application).payload
    twin = freeze(ctx.contract, task, items, "cwa", ctx.config.application) if arm == "xml-unescaped" else None
    return baselines.build(arm, task, items, ctx.config.application.tokenizer,
                           twin.payload if twin else None).payload


def run(ctx, cells: list[dict], tasks_by_id: dict, tally) -> list[dict]:
    config = ctx.config
    tokenizer = Tokenizer(config)
    try:
        ids = tokenizer.marker_ids(config.attacks.chat_template)
    except TokenizeError as error:
        ctx.log(f"S1: tokenize: {error}")
        return []
    if not ids:
        ctx.log("S1: tokenize: no marker ids cached or served")
        return []
    per_family = defaultdict(int)
    chosen = []
    for cell in cells:
        if cell["kind"] == "attacked" and cell["carrier"] == "marker":
            family = tasks_by_id[cell["task_id"]]["family"]
            if per_family[family] < config.tokenize_cells:
                per_family[family] += 1
                chosen.append(cell)
    rows = []
    for cell in chosen:
        task = tasks_by_id[cell["task_id"]]
        for arm in config.arms:
            probe = grid.probe(cell, task, arm, config.attacks.seed, config.attacks.chat_template)
            try:
                attacked = tokenizer.specials(_payload(ctx, task, probe["items"], arm), ids)
                clean = tokenizer.specials(_payload(ctx, task, task["items"], arm), ids)
            except TokenizeError as error:
                ctx.log(f"S1: tokenize {cell['cell_id']}@{arm}: {error}")
                attacked = clean = None
            extra = None
            if attacked is not None and clean is not None:
                extra = sum(attacked.values()) - sum(clean.values())
                tally["special_tokens"][0] += extra > 0
                tally["special_tokens"][1] += 1
            rows.append({"$schema": output.schema_name("token-row"), "run_id": ctx.run.run_id, "suite": "S1",
                         "case_id": f"{cell['cell_id']}@{arm}", "cell": cell["cell_id"], "arm": arm,
                         "template": config.attacks.chat_template, "model": config.tokenize_model,
                         "attacked": attacked, "clean": clean, "extra": extra})
    return rows

