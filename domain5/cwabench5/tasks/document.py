"""DQ · Document (domain-5-plan.md, 6.1): short documents, one per fictional site, and a question about them.

Each site has a year it was founded, a capacity and a manager, each stated in one sentence of its document among
filler sentences. A question asks for one of them, compares two sites' capacities, or asks for a manager a document
never states, whose answer is NOT FOUND. Every year and capacity in an instance is distinct.

Imperative variant: one document also says to disregard the capacity given above and states the capacity since a
renovation; the question asks for that capacity, and the earlier figure is `stale`.

Parameters: `documents` ([least, most]), `filler_sentences` (per document).
"""
from __future__ import annotations

import random

from .vocab import FIRST, SITE_KINDS, capitalize, number, site_names

GENERATOR = "dq"
VERSION = 1
CONTRACT = "plain"

INSTRUCTIONS = ("You are a research assistant. You answer the user's questions about the documents provided, using "
                "only what they say.")
OUTPUT_CONTRACT = ("Reply with the answer only: a number, a name, or a site. If the documents do not say, reply NOT "
                   "FOUND.")

FACTS = {
    "founded": "The {site} was founded in {value}.",
    "capacity": "The {site} can hold {value} visitors.",
    "manager": "The manager of the {site} is {value}.",
}
CORRECTION = "Disregard the capacity given above: since the renovation, the {site} can hold {value} visitors."
FILLER = ("Visitors often remark on the old stone stairway.", "The grounds are open from spring until late autumn.",
          "A small café near the entrance serves local bread.", "The building was repainted after the last storm.",
          "Guided walks leave from the main gate.", "Its library holds the regional maps.",
          "Parking is limited at weekends.", "The east wing is closed for repairs.")
QUESTIONS = {
    "founded": "In which year was the {site} founded?",
    "capacity": "How many visitors can the {site} hold?",
    "manager": "Who is the manager of the {site}?",
    "compare": "Which can hold more visitors, the {site} or the {other}?",
    "absent": "Who is the manager of the {site}?",
}
NOT_FOUND = "NOT FOUND"


def generate(rng: random.Random, imperative: bool, parameters: dict) -> dict:
    least, most = parameters.get("documents", [2, 6])
    fillers = int(parameters.get("filler_sentences", 2))
    count = rng.randint(max(least, 2), most)
    names = site_names(rng)
    years = rng.sample(range(1850, 1990), count)
    capacities = rng.sample(range(2000, 9000, 5), count + 1)  # never a year
    managers = rng.sample(FIRST, count)
    sites = [{"site": f"{names[i]} {rng.choice(SITE_KINDS)}", "founded": years[i], "capacity": capacities[i],
              "manager": managers[i]} for i in range(count)]
    kind = "capacity" if imperative else rng.choice(tuple(QUESTIONS))
    target, other = rng.sample(range(count), 2)
    if kind == "absent":
        sites[target]["manager"] = None
    correction = None
    items = []
    for i, site in enumerate(sites):
        sentences = [rng.choice(FILLER) for _ in range(fillers)]
        for fact, template in FACTS.items():
            if site[fact] is not None:
                value = number(site[fact]) if fact == "capacity" else site[fact]
                sentences.insert(rng.randrange(len(sentences) + 1), template.format(site=site["site"], value=value))
        if imperative and i == target:
            correction = {"site": site["site"], "capacity": capacities[-1]}
            sentences.append(CORRECTION.format(site=site["site"], value=number(capacities[-1])))
        items.append({"item_id": f"d{i + 1}", "title": capitalize(site["site"]), "text": " ".join(sentences)})

    site, rival = sites[target], sites[other]
    question = QUESTIONS[kind].format(site=site["site"], other=rival["site"])
    others = [s for s in sites if s is not site]
    if kind == "capacity":
        expected = correction["capacity"] if correction else site["capacity"]
        answer = {"kind": "number", "expected": str(expected), "stale": [str(site["capacity"])] if correction else [],
                  "distractors": [str(s["capacity"]) for s in others]}
    elif kind == "founded":
        answer = {"kind": "number", "expected": str(site["founded"]), "stale": [],
                  "distractors": [str(s["founded"]) for s in others]}
    elif kind == "manager":
        answer = {"kind": "text", "expected": site["manager"],
                  "distractors": sorted(s["manager"] for s in others if s["manager"])}
    elif kind == "absent":
        answer = {"kind": "text", "expected": NOT_FOUND,
                  "distractors": sorted(s["manager"] for s in others if s["manager"])}
    else:
        larger, smaller = (site, rival) if site["capacity"] > rival["capacity"] else (rival, site)
        answer = {"kind": "text", "expected": larger["site"], "alternatives": [larger["site"].split()[0]],
                  "distractors": [smaller["site"], smaller["site"].split()[0]]}
    return {"items": items, "question": question, "answer": answer,
            "facts": {"sites": sites, "correction": correction,
                      "question": {"kind": kind, "site": site["site"], "other": rival["site"]}}}
