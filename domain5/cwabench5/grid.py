"""The probes a run sends (domain-5-plan.md, 8, 9): S1 gates them and S2 will send them.

A cell is one probe before any arm writes it:

- **attacked** (the main grid): every plain instance × every test-half template that applies to it × every position,
  with the carrier balanced: the `n`-th template at the `p`-th position of the run's `k`-th instance takes the
  applicable text carrier numbered `n + p + k`, so every carrier meets every template, position and family;
- **clean**: every instance, plain and imperative, with no attack: benign utility and over-refusal (section 6.1);
- **html** (the representation sweep): every plain DQ instance × the first `[grid].html_templates` test templates ×
  every HTML carrier × both producers, the position rotating.

`probe(cell, task, arm, …)` writes a cell for one arm. Only the `tag` carrier differs between arms: it forges the
arm's own delimiters (arms.py), so its probe is composed per arm; every other cell is the same for every arm.
"""
from __future__ import annotations

from . import attacks
from .arms import ARMS
from .attacks import carriers
from .attacks import html as html_attacks
from .tasks.html import PRODUCERS


def _templates(families: list[str]) -> list:
    return [t for t in attacks.every_template() if t.split == "test" and t.family in families]


def cells(instances: dict[str, list[dict]], families: list[str], html_templates: int) -> list[dict]:
    found = []
    plain = [t for family in instances.values() for t in family if t["variant"] == "plain"]
    templates = _templates(families)
    for k, task in enumerate(plain):
        for n, template in enumerate(templates):
            if not template.applies_to(task["family"]):
                continue
            usable = [c for c in carriers.CARRIERS if carriers.applies(c, template.family)]
            for p, position in enumerate(attacks.POSITIONS):
                carrier = usable[(n + p + k) % len(usable)]
                found.append({"cell_id": f"{task['task_id']}/{template.template_id}/{position}/{carrier}",
                              "kind": "attacked", "task_id": task["task_id"], "template_id": template.template_id,
                              "position": position, "carrier": carrier, "producer": None})
    for family in instances.values():
        for task in family:
            found.append({"cell_id": f"{task['task_id']}/clean", "kind": "clean", "task_id": task["task_id"],
                          "template_id": None, "position": None, "carrier": None, "producer": None})
    chosen = [t for t in templates if t.applies_to("dq")][:html_templates]
    for k, task in enumerate(t for t in instances.get("dq", []) if t["variant"] == "plain"):
        for n, template in enumerate(chosen):
            for c, carrier in enumerate(html_attacks.CARRIERS):
                position = attacks.POSITIONS[(n + c + k) % len(attacks.POSITIONS)]
                for producer in PRODUCERS:
                    found.append({"cell_id": f"{task['task_id']}/{template.template_id}/{position}/html-{carrier}/"
                                             f"{producer}",
                                  "kind": "html", "task_id": task["task_id"], "template_id": template.template_id,
                                  "position": position, "carrier": carrier, "producer": producer})
    return found


_TEMPLATES = {t.template_id: t for t in attacks.every_template()}


def probe(cell: dict, task: dict, arm: str, seed: int, chat_template: str) -> dict:
    """The cell written for `arm`. A clean cell's probe holds the task's own items and no attack."""
    if cell["kind"] == "clean":
        return {"probe_id": cell["cell_id"], "task_id": task["task_id"], "template_id": None, "family": None,
                "position": None, "carrier": None, "producer": None, "item_id": None, "instruction": None,
                "attack_text": None, "inserted": None, "items": task["items"], "answer": task["answer"],
                "predicate": None}
    template = _TEMPLATES[cell["template_id"]]
    if cell["kind"] == "html":
        return html_attacks.compose(task, template, cell["position"], cell["carrier"], cell["producer"], seed)
    delimiters = ARMS[arm].delimiters if cell["carrier"] == "tag" else None
    return attacks.compose(task, template, cell["position"], cell["carrier"], seed, delimiters=delimiters,
                           chat_template=chat_template)
