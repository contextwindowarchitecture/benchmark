"""HTML carriers: where an attack hides in a web page (domain-5-plan.md, 6.3, 6.4).

Most injections found on the web sit in markup a reader never sees. Each carrier writes the instruction into one
page of a DQ instance's documents (tasks/html.py), and a producer then turns every page into an evidence body:

- `comment`: an HTML comment between paragraphs;
- `meta`: a `<meta name="keywords">` element in the head (the position is always the head's);
- `hidden`: a paragraph styled `display:none`;
- `alt`: an image whose alt text is the instruction.

`compose` makes a probe as attacks/compose.py does, from the same template fill and the same probe seed, with the
pages kept beside the produced bodies: `inserted` is what was written into the target page, so removing it gives the
page back, and `attack_text` is what an arm finds in its payload when the producer let the attack through.
"""
from __future__ import annotations

import html
import random

from ..tasks.html import page, produce
from .compose import _fill, probe_seed
from .families import Template

CARRIERS = ("comment", "meta", "hidden", "alt")


def construct(carrier: str, instruction: str) -> str:
    if carrier == "comment":
        return f"<!-- {instruction} -->"
    if carrier == "meta":
        return f'<meta name="keywords" content="{html.escape(instruction)}">'
    if carrier == "hidden":
        return f'<p style="display:none">{html.escape(instruction, quote=False)}</p>'
    if carrier == "alt":
        return f'<img src="banner.png" alt="{html.escape(instruction)}">'
    raise ValueError(f"unknown HTML carrier {carrier!r}")


def _insert(source: str, block: str, position: str, carrier: str) -> tuple[str, str]:
    if carrier == "meta":
        at = source.index("</head>")
        inserted = block + "\n"
        return source[:at] + inserted + source[at:], inserted
    starts = [i for i in range(len(source)) if source.startswith("<p>", i)]
    if position == "start":
        at = starts[0]
    elif position == "end":
        at = source.index("</body>")
    else:
        at = starts[len(starts) // 2] if len(starts) > 1 else source.index("</body>")
    inserted = block + "\n"
    return source[:at] + inserted + source[at:], inserted


def compose(task: dict, template: Template, position: str, carrier: str, producer: str, seed: int) -> dict:
    if task["family"] != "dq" or not template.applies_to("dq"):
        raise ValueError("HTML carriers are written into the document family's pages")
    own_seed = probe_seed(seed, task["task_id"], template.template_id, position, f"html-{carrier}")
    rng = random.Random(own_seed)
    fill, predicate = _fill(template, task, rng)
    instruction = template.text(**fill)
    block = construct(carrier, instruction)
    pages = {i["item_id"]: page(i["title"], i["text"]) for i in task["items"]}
    target = rng.choice(list(pages))
    pages[target], inserted = _insert(pages[target], block, position, carrier)
    items = [{**i, "text": produce(producer, pages[i["item_id"]])} for i in task["items"]]
    return {
        "probe_id": f"{task['task_id']}/{template.template_id}/{position}/html-{carrier}/{producer}",
        "task_id": task["task_id"],
        "template_id": template.template_id,
        "family": template.family,
        "split": template.split,
        "position": "head" if carrier == "meta" else position,
        "carrier": f"html-{carrier}",
        "producer": producer,
        "seed": own_seed,
        "item_id": target,
        "instruction": instruction,
        "attack_text": block,
        "inserted": inserted,
        "pages": pages,
        "items": items,
        "answer": dict(task["answer"]),
        "predicate": {"family": template.family, **predicate},
    }
