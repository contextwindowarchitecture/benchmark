"""Pasted material (S5, domain-5-plan.md, 8): the content in the user's own message, as R-10 describes it.

The user pastes what they were sent (the probe's items, joined as the baselines join content) above their question,
and the application may delimit it. Every arm sends the same message body, `body(...)`:

| Arm | The user's message |
| --- | --- |
| `pasted-naive` | PASTE_INTRO, the material, the question |
| `pasted-delimited` | PASTE_INTRO, the material between PASTE_OPEN and PASTE_CLOSE, PASTE_SENTENCE, the question |
| `pasted-nonce` | as `pasted-delimited`, the boundary carrying the request's code (`code`) |
| `cwa-pasted` | `pasted-naive`'s body as the `interaction.query` item |
| `cwa-pasted-nonce` | `pasted-nonce`'s body as the `interaction.query` item |

The baselines' system prompt is the instructions and the output contract (baselines.py). A CWA arm's snapshot holds
the governance items and the query, and no evidence: the assembler never splits a body, so the material renders
inside the query's element, escaped (SPEC.md, R-10). The controlled pairs: `pasted-naive` → `cwa-pasted` (CWA's
rendering), `cwa-pasted` → `cwa-pasted-nonce` (the delimiting R-10 recommends), `pasted-nonce` → `cwa-pasted-nonce`
(rendering, both delimited), `pasted-delimited` → `pasted-nonce` (a boundary the material can imitate, or not).

The code is the first 12 hex digits of SHA-256 over the run's attack seed and the cell, the same in both nonce arms;
an attack never contains it, since the attack is composed without it.
"""
from __future__ import annotations

import hashlib
import json

from cwabench.canon import jcs, render
from cwabench.canon.tokenizers import TOKENIZERS
from cwabench2.application import writers

from .application import route as route_mod
from .application.snapshot import ASKED, START, Frozen, Settings, governance, snapshot
from .arms import PASTE_CLOSE, PASTE_INTRO, PASTE_OPEN, PASTE_SENTENCE, PASTED, nonce_markers
from .baselines import Built
from .handoff import texts

ARMS = tuple(PASTED)
QUERY = "question:pasted"


def code(seed: int, cell_id: str) -> str:
    return hashlib.sha256(f"{seed}:{cell_id}:paste".encode()).hexdigest()[:12]


def markers(arm: str, code_: str) -> tuple[str, str] | None:
    if arm == "pasted-delimited":
        return PASTE_OPEN, PASTE_CLOSE
    if arm in ("pasted-nonce", "cwa-pasted-nonce"):
        return nonce_markers(code_)
    return None


def body(arm: str, task: dict, items: list[dict], code_: str) -> str:
    material = "\n\n".join(i["text"] for i in items)
    found = markers(arm, code_)
    if found is None:
        return f"{PASTE_INTRO}\n\n{material}\n\n{task['question']}"
    opening, closing = found
    return (f"{PASTE_INTRO}\n{opening}\n{material}\n{closing}\n"
            f"{PASTE_SENTENCE.format(open=opening, close=closing)}\n\n{task['question']}")


def build(arm: str, task: dict, items: list[dict], code_: str, tokenizer: str) -> Built:
    system = f"{task['instructions']}\n\n{task['output_contract']}"
    payload = jcs.serialize_bytes({"system": [{"id": "system", "text": system}], "tools": [],
                                   "messages": [{"role": "user", "content": body(arm, task, items, code_)}]})
    return Built(arm, payload, sum(TOKENIZERS[tokenizer](t) for t in texts(payload)))


def freeze(contract, task: dict, items: list[dict], arm: str, settings: Settings, code_: str) -> Frozen:
    """A CWA pasted arm's snapshot: governance and the query holding the material; `content` maps every pasted item to
    the query, where its text now is."""
    governed = governance(contract, task, "cwa")
    query = writers.item(contract, QUERY, "interaction.query", body(arm, task, items, code_),
                         source=f"question:{task['task_id']}", freshness=ASKED, injection_risk="untrusted_content")
    batches = [{"producer": {"id": "policy-registry", "kind": "policy"}, "items": governed, "excluded": []},
               {"producer": {"id": "conversation", "kind": "interaction"}, "items": [query], "excluded": []}]
    made = snapshot(contract, task, "cwa", settings, batches)  # the `cwa` arm's profile, on the pasted route
    made["route_policy"] = route_mod.route_policy(contract, pasted=True)
    made["profile"] = route_mod.profile(contract, route_mod.PROFILE_OF["cwa"], pasted=True)
    every = [*governed, query]
    rendered = render.render(made, every)
    tokens = rendered.count(settings.tokenizer)
    full = render.charged(tokens, settings.margin_percent)
    made["budget"]["input"] = full
    return Frozen(made, every, {i["item_id"]: QUERY for i in items}, rendered.payload, tokens, full)


def forged(arm: str, payload: bytes, code_: str) -> bool | None:
    """Whether the material wrote the arm's boundary itself: a delimiting baseline's boundary string more often than the
    boundary line and the sentence naming it write it (twice)."""
    found = markers(arm, code_)
    if found is None or PASTED[arm].kind == "cwa":
        return None
    content = json.loads(payload.decode("utf-8"))["messages"][0]["content"]
    return any(content.count(m) > 2 for m in found)


__all__ = ["ARMS", "START", "build", "body", "code", "forged", "freeze"]
