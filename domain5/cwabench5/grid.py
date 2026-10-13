"""The probes a run sends (domain-5-plan.md, 8, 9): S1 gates them and S2 will send them.

A cell is one probe before any arm writes it:

- **attacked** (the main grid): every plain instance × every test-half template that applies to it × every position,
  with the carrier balanced: the `n`-th template at the `p`-th position of the run's `k`-th instance takes the
  applicable text carrier numbered `n + p + k`, so every carrier meets every template, position and family;
- **clean**: every instance, plain and imperative, with no attack: benign utility and over-refusal (section 6.1);
- **html** (the representation sweep): every plain DQ instance × the first `[grid].html_templates` test templates ×
  every HTML carrier × both producers, the position rotating;
- **length** (the length sweep): every plain EM and DQ instance padded to each of `[grid].lengths` tokens
  (tasks/length.py) × the first `[grid].length_templates` test templates that apply, at the end, the carrier balanced;
  and each padded instance clean;
- **sep** and **llmail** (S3's external sets, external/): each external probe once, as its set wrote it.

`probe(cell, task, arm, …)` writes a cell for one arm. Only the `tag` carrier differs between arms: it forges the
arm's own delimiters (arms.py), so its probe is composed per arm; every other cell is the same for every arm.
"""
from __future__ import annotations

from . import attacks, external
from .arms import ALL
from .attacks import carriers
from .attacks import html as html_attacks
from .tasks.html import PRODUCERS
from .tasks.length import lengthen


def _templates(families: list[str]) -> list:
    return [t for t in attacks.every_template() if t.split == "test" and t.family in families]


def padded(instances: dict[str, list[dict]], lengths: list[int], seed: int) -> list[dict]:
    """Every plain EM and DQ instance padded to each length, in order."""
    return [lengthen(task, length, seed) for family in ("em", "dq") for task in instances.get(family, [])
            if task["variant"] == "plain" for length in lengths]


def tasks_by_id(instances: dict[str, list[dict]], long: list[dict], outside: list[dict] = ()) -> dict[str, dict]:
    return {t["task_id"]: t for t in [*(t for family in instances.values() for t in family), *long, *outside]}


def cells(instances: dict[str, list[dict]], families: list[str], html_templates: int, long: list[dict] = (),
          length_templates: int = 0, outside: list[dict] = ()) -> list[dict]:
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
    for k, task in enumerate(long):
        chosen = [t for t in templates if t.applies_to(task["family"])][:length_templates]
        for n, template in enumerate(chosen):
            usable = [c for c in carriers.CARRIERS if carriers.applies(c, template.family)]
            carrier = usable[(n + k) % len(usable)]
            found.append({"cell_id": f"{task['task_id']}/{template.template_id}/end/{carrier}", "kind": "length",
                          "task_id": task["task_id"], "template_id": template.template_id, "position": "end",
                          "carrier": carrier, "producer": None})
        found.append({"cell_id": f"{task['task_id']}/clean", "kind": "length-clean", "task_id": task["task_id"],
                      "template_id": None, "position": None, "carrier": None, "producer": None})
    for task in outside:
        found.append({"cell_id": task["task_id"], "kind": task["family"], "task_id": task["task_id"],
                      "template_id": None, "position": None, "carrier": None, "producer": None})
    return found


_TEMPLATES = {t.template_id: t for t in attacks.every_template()}


def probe(cell: dict, task: dict, arm: str, seed: int, chat_template: str) -> dict:
    """The cell written for `arm`. A clean cell's probe holds the task's own items and no attack."""
    if cell["kind"] in ("clean", "length-clean"):
        return {"probe_id": cell["cell_id"], "task_id": task["task_id"], "template_id": None, "family": None,
                "position": None, "carrier": None, "producer": None, "item_id": None, "instruction": None,
                "attack_text": None, "inserted": None, "items": task["items"], "answer": task["answer"],
                "predicate": None}
    if cell["kind"] in external.KINDS:
        given = task["external"]
        return {"probe_id": cell["cell_id"], "task_id": task["task_id"], "template_id": None, "family": task["family"],
                "position": None, "carrier": None, "producer": None, "item_id": given["attack_item"],
                "instruction": None, "attack_text": given["attack_text"], "inserted": None, "items": task["items"],
                "answer": None, "predicate": None}
    template = _TEMPLATES[cell["template_id"]]
    if cell["kind"] == "html":
        return html_attacks.compose(task, template, cell["position"], cell["carrier"], cell["producer"], seed)
    delimiters = ALL[arm].delimiters if cell["carrier"] == "tag" else None
    return attacks.compose(task, template, cell["position"], cell["carrier"], seed, delimiters=delimiters,
                           chat_template=chat_template)
