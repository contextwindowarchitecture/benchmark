"""TQ · Table (domain-5-plan.md, 6.1): an inventory table rendered as text, and a lookup, count or sum over it.

Rows pair a product with a region, each pair once, with a quantity and a unit price; every quantity is distinct. A
question asks for one row's quantity, how many rows list a region, or a region's total quantity.

Imperative variant: one of a region's rows is repeated at the end of the table, and a note below it says to ignore
that row, entered twice; the question counts or sums the region, and the answer with the repeat included is `stale`.

Parameters: `rows` ([least, most]).
"""
from __future__ import annotations

import random

from .vocab import PRODUCTS, REGIONS

GENERATOR = "tq"
VERSION = 1
CONTRACT = "plain"

INSTRUCTIONS = ("You are a data assistant. You answer the user's questions about the table provided, reading it "
                "exactly.")
OUTPUT_CONTRACT = "Reply with the number only."

HEADER = "| Product | Region | Quantity | Unit price |\n| --- | --- | --- | --- |"
NOTE = "Note: ignore the row for the {product} in the {region} region at the end of the table; it was entered twice."
QUESTIONS = {
    "lookup": "What quantity of {product} is listed for the {region} region?",
    "count": "How many rows list the {region} region?",
    "sum": "What is the total quantity listed for the {region} region?",
}


def _row(row: dict) -> str:
    return f"| {row['product']} | {row['region']} | {row['quantity']} | {row['price']} |"


def generate(rng: random.Random, imperative: bool, parameters: dict) -> dict:
    least, most = parameters.get("rows", [10, 40])
    pairs = [(p, r) for p in PRODUCTS for r in REGIONS]
    count = min(rng.randint(max(least, 6), most), len(pairs))
    chosen = rng.sample(pairs, count)
    quantities = rng.sample(range(1, 600), count)
    rows = [{"product": p, "region": r, "quantity": q, "price": rng.randint(2, 90)}
            for (p, r), q in zip(chosen, quantities)]
    kind = rng.choice(("count", "sum")) if imperative else rng.choice(tuple(QUESTIONS))
    target = rng.choice(rows)
    region = target["region"]
    lines = [HEADER, *(_row(r) for r in rows)]
    repeat = None
    if imperative:
        repeat = dict(target)
        lines += [_row(repeat), "", NOTE.format(product=repeat["product"], region=region)]
    in_region = [r for r in rows if r["region"] == region]

    def value(of: str, members: list[dict]) -> int:
        return len(members) if of == "count" else sum(r["quantity"] for r in members)

    if kind == "lookup":
        expected = target["quantity"]
        stale, distractors = [], [r["quantity"] for r in in_region if r is not target]
    else:
        expected = value(kind, in_region)
        stale = [value(kind, in_region + [repeat])] if repeat else []
        distractors = sorted({value(kind, [r for r in rows if r["region"] == other])
                              for other in REGIONS if other != region} - {expected, *stale, 0})
    question = QUESTIONS[kind].format(product=target["product"], region=region)
    answer = {"kind": "number", "expected": str(expected), "stale": [str(s) for s in stale],
              "distractors": [str(d) for d in distractors if d != expected and d not in stale]}
    items = [{"item_id": "t1", "title": "Inventory", "text": "\n".join(lines)}]
    return {"items": items, "question": question, "answer": answer,
            "facts": {"rows": rows, "repeat": repeat,
                      "question": {"kind": kind, "product": target["product"], "region": region}}}
