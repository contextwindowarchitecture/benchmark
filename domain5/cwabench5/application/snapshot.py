"""A CWA arm's frozen snapshot for one probe, and what assembly must make of it (domain-5-plan.md, 4.3).

**The items.** The task's instructions (with REMINDER appended in the arms that add it) and output contract,
protected and verified, from the policy registry; each content item as its own evidence item, in the order the probe
holds them, with the authority its slot takes and `injection_risk: untrusted_content` (R-1, R-10); and the question as
the query. Content is `evidence.knowledge` from the content index (relevance 1.0: every item was retrieved for the
question), or `evidence.tool_results` from the content tool in `cwa-observation`. Item ids number the content
(`content:01`, …), since the specification renders a placement's items in id order and the content must read in its
own order. Every item writes its six policy fields out (R-3), so a trace fills no defaults.

**The budget** is the snapshot's full charged count: nothing is shed, so every arm sends all the content and the
attack-in-payload oracle can only find the attack present (domain-5-plan.md, 3). Flooding is S1's own test.

**The prediction.** Every item is admitted, and the payload is what the published renderer writes for all of them
(cwabench.canon.render, which S0 of Domain 1 checks against every published payload).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta

from cwabench.canon import render
from cwabench2.application import writers
from cwabench2.application.snapshots import Settings
from cwabench2.application.writers import START, TENANT, USER

from ..arms import REMINDER
from . import route as route_mod

ASKED = START + timedelta(hours=1)  # the content is retrieved at START; the question is asked an hour later
BOUNDARY_ARMS = ("cwa-boundary", "cwa-reinforced", "cwa-observation")

__all__ = ["ASKED", "Frozen", "Settings", "content_id", "freeze"]


def content_id(n: int) -> str:
    return f"content:{n:02d}"


@dataclass
class Frozen:
    snapshot: dict
    items: list[dict]
    content: dict[str, str]  # a probe item's id → its evidence item's id
    payload: bytes  # the prediction
    input_tokens: int
    full: int  # the charged count of every item: the budget

    def at(self, budget: int | None = None) -> bytes:
        snapshot = dict(self.snapshot, budget=dict(self.snapshot["budget"], input=budget or self.full))
        return json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    @property
    def payload_hash(self) -> str:
        return hashlib.sha256(self.payload).hexdigest()

    @property
    def included(self) -> list[str]:
        return sorted(i["id"] for i in self.items)


def governance(contract, task: dict, arm: str) -> list[dict]:
    instructions = task["instructions"] + (f" {REMINDER}" if arm in BOUNDARY_ARMS else "")
    return [
        writers.item(contract, "policy:instructions", "governance.instructions", instructions,
                     source="policy-registry:instructions", freshness=START, trust="verified"),
        writers.item(contract, "policy:output-contract", "governance.output_contract", task["output_contract"],
                     source="policy-registry:output-contract", freshness=START, trust="verified"),
    ]


def evidence(contract, task: dict, items: list[dict], observation: bool) -> tuple[list[dict], dict[str, str]]:
    slot = "evidence.tool_results" if observation else "evidence.knowledge"
    found, ids = [], {}
    for n, item in enumerate(items, 1):
        ids[item["item_id"]] = content_id(n)
        extra = {} if observation else {"relevance": 1.0}
        found.append(writers.item(contract, content_id(n), slot, item["text"],
                                  source=f"content:{task['task_id']}:{item['item_id']}", freshness=START,
                                  scope={"tenant": TENANT}, injection_risk="untrusted_content", **extra))
    return found, ids


def snapshot(contract, task: dict, arm: str, settings: Settings, batches: list[dict]) -> dict:
    return {
        "assembly_time": writers.instant(ASKED),
        "scope": {"tenant": TENANT, "user": USER, "session": task["task_id"], "task": task["task_id"]},
        "budget": {"input": 0, "reserved_output": settings.reserved_output, "margin_percent": settings.margin_percent},
        "profile": route_mod.profile(contract, route_mod.PROFILE_OF[arm]),
        "route_policy": route_mod.route_policy(contract),
        "tokenizer": settings.tokenizer,
        "renderer": settings.renderer,
        "batches": batches,
        "conflicts": [],
    }


def freeze(contract, task: dict, items: list[dict], arm: str, settings: Settings) -> Frozen:
    """`arm`'s snapshot of `task` with `items` as its content (a probe's attacked items, or the task's own)."""
    observation = arm == "cwa-observation"
    governed = governance(contract, task, arm)
    query = writers.item(contract, f"question:{task['task_id']}", "interaction.query", task["question"],
                         source=f"question:{task['task_id']}", freshness=ASKED)
    content, ids = evidence(contract, task, items, observation)
    batches = [
        {"producer": {"id": "policy-registry", "kind": "policy"}, "items": governed, "excluded": []},
        {"producer": {"id": "conversation", "kind": "interaction"}, "items": [query], "excluded": []},
        {"producer": {"id": "content-tool", "kind": "mcp"} if observation else
         {"id": "content-index", "kind": "retrieval"}, "items": content, "excluded": []},
    ]
    made = snapshot(contract, task, arm, settings, batches)
    every = [*governed, query, *content]
    rendered = render.render(made, every)
    tokens = rendered.count(settings.tokenizer)
    full = render.charged(tokens, settings.margin_percent)
    made["budget"]["input"] = full
    return Frozen(made, every, ids, rendered.payload, tokens, full)
