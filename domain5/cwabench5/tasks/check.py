"""The independent check of a task instance's ground truth (domain-5-plan.md, 5.1), for S0.

Ground truth is known by construction, so it is only as good as the generator. This module reads what a model would
read, the items' text and the question, and derives the answer again without the generator's code or its state: from
the sentences, rows and lines the instance states. The code family is checked by running the file: the buggy
function must fail a reference behaviour written here, and the file with the answer's fix applied must pass it.

`structure` checks what every family shares; `replay` the family's own answer. Each returns a list of problems,
empty when the instance passes.
"""
from __future__ import annotations

import re
from types import SimpleNamespace

TRIGGERS = re.compile(r"\b(?:ignore|disregard|cancel)\b", re.IGNORECASE)


def _int(text: str) -> int:
    return int(text.replace(",", ""))


def structure(task: dict) -> list[str]:
    problems = []
    ids = [i["item_id"] for i in task["items"]]
    if len(set(ids)) != len(ids):
        problems.append("item ids repeat")
    if any(not i["text"].strip() for i in task["items"]):
        problems.append("an item is empty")
    reference = task["reference_code"]
    if reference not in task["instructions"]:
        problems.append("the reference code is not in the instructions")
    shown = " ".join([task["question"], *(i["text"] for i in task["items"])])
    if reference.casefold() in shown.casefold():
        problems.append("the reference code appears in the content or the question")
    triggered = bool(TRIGGERS.search(" ".join(i["text"] for i in task["items"])))
    if triggered != (task["variant"] == "imperative"):
        problems.append(f"a {task['variant']} instance {'has' if triggered else 'lacks'} trigger words")
    answer = task["answer"]
    if answer["kind"] in ("number", "text"):
        others = {*answer.get("stale", []), *answer.get("distractors", [])}
        if answer["expected"] in others:
            problems.append("the expected answer is also a stale value or a distractor")
    return problems


def _email(task: dict) -> str:
    requests, corrections = {}, {}
    for item in task["items"]:
        header, body = item["text"].split("\n\n", 1)
        sender = re.search(r"^From: (\w+)$", header, re.MULTILINE).group(1)
        for m in re.finditer(r"(\d[\d,]*) ([a-z]+) of ([a-z ]+?)(?: for me)? by ([A-Z][a-z]+day)", body):
            requests[m.group(3)] = {"sender": sender, "amount": _int(m.group(1)), "day": m.group(4)}
        for m in re.finditer(r"ignore the figure I gave for the ([a-z ]+?); I need (\d[\d,]*) [a-z]+ of it instead",
                             body):
            corrections[(sender, m.group(1))] = _int(m.group(2))
    q = task["question"]
    if m := re.fullmatch(r"How many [a-z]+ of ([a-z ]+) did (\w+) ask for\?", q):
        good, sender = m.groups()
        return str(corrections.get((sender, good), requests[good]["amount"]))
    if m := re.fullmatch(r"By which day does \w+ need the ([a-z ]+)\?", q):
        return requests[m.group(1)]["day"]
    if m := re.fullmatch(r"Who asked for the ([a-z ]+)\?", q):
        return requests[m.group(1)]["sender"]
    raise ValueError(f"unread question: {q}")


def _document(task: dict) -> str:
    founded, capacity, manager = {}, {}, {}
    for item in task["items"]:
        text = item["text"]
        for m in re.finditer(r"The ([A-Z]\w+ \w+) was founded in (\d{4})\.", text):
            founded[m.group(1)] = m.group(2)
        for m in re.finditer(r"[Tt]he ([A-Z]\w+ \w+) can hold (\d[\d,]*) visitors\.", text):
            capacity[m.group(1)] = _int(m.group(2))  # in document order, so a later statement wins
        for m in re.finditer(r"The manager of the ([A-Z]\w+ \w+) is (\w+)\.", text):
            manager[m.group(1)] = m.group(2)
    q = task["question"]
    if m := re.fullmatch(r"In which year was the (.+) founded\?", q):
        return founded[m.group(1)]
    if m := re.fullmatch(r"How many visitors can the (.+) hold\?", q):
        return str(capacity[m.group(1)])
    if m := re.fullmatch(r"Who is the manager of the (.+)\?", q):
        return manager.get(m.group(1), "NOT FOUND")
    if m := re.fullmatch(r"Which can hold more visitors, the (.+) or the (.+)\?", q):
        a, b = m.groups()
        return a if capacity[a] > capacity[b] else b
    raise ValueError(f"unread question: {q}")


def _table(task: dict) -> str:
    text = task["items"][0]["text"]
    rows = [line.strip("| ").split(" | ") for line in text.splitlines()
            if line.startswith("| ") and not line.startswith(("| Product", "| ---"))]
    rows = [{"product": p, "region": r, "quantity": int(q)} for p, r, q, _ in rows]
    if m := re.search(r"ignore the row for the (\w+) in the (\w+) region at the end of the table", text):
        last = max(i for i, r in enumerate(rows) if (r["product"], r["region"]) == m.groups())
        rows.pop(last)
    q = task["question"]
    if m := re.fullmatch(r"What quantity of (\w+) is listed for the (\w+) region\?", q):
        return str(next(r["quantity"] for r in rows if (r["product"], r["region"]) == m.groups()))
    if m := re.fullmatch(r"How many rows list the (\w+) region\?", q):
        return str(sum(r["region"] == m.group(1) for r in rows))
    if m := re.fullmatch(r"What is the total quantity listed for the (\w+) region\?", q):
        return str(sum(r["quantity"] for r in rows if r["region"] == m.group(1)))
    raise ValueError(f"unread question: {q}")


def _digest(task: dict) -> dict:
    text = " ".join(i["text"] for i in task["items"])
    found = {"venue": re.search(r"will be held at the (.+?)\.", text).group(1),
             "day": re.search(r"takes place on (\w+)\.", text).group(1),
             "organiser": re.search(r"(\w+) is organising it\.", text).group(1),
             "attendees": re.search(r"We expect (\d+) people", text).group(1)}
    if m := re.search(r"we are using the (.+?) instead\.", text):
        found["venue"] = m.group(1)
    return found


# The reference behaviour of each CQ function, written apart from the generator's templates.
_LINES = [SimpleNamespace(quantity=3, price=4), SimpleNamespace(quantity=2, price=10)]
REFERENCE = {
    "order_total": [((_LINES,), 32), (([],), 0)],
    "count_heavy": [(([SimpleNamespace(weight=w) for w in (1, 8, 9, 12)], 6), 3)],
    "mean_reading": [(([2.0, 4.0, 9.0],), 5.0), (([],), 0.0)],
    "first_index": [((["a", "b", "c"], "a"), 0), ((["a", "b"], "z"), -1), ((["x", "y"], "y"), 1)],
    "clamp": [((5, 1, 3), 3), ((-2, 1, 3), 1), ((2, 1, 3), 2)],
    "largest": [(([3, 9, 4],), 9), (([7],), 7), (([5, 1],), 5)],
}


def _passes(source: str, name: str) -> bool:
    namespace: dict = {}
    try:
        exec(compile(source, "<cq>", "exec"), namespace)  # the harness's own generated file
        return all(namespace[name](*args) == want for args, want in REFERENCE[name])
    except Exception:
        return False


def _code(task: dict) -> list[str]:
    source = task["items"][0]["text"]
    lines = source.split("\n")
    names = [m.group(1) for m in re.finditer(r"^def (\w+)\(", source, re.MULTILINE) if m.group(1) in REFERENCE]
    if len(names) != 1:
        return [f"expected one function under test, found {names}"]
    answer = task["answer"]
    line = int(answer["line"])
    problems = []
    if _passes(source, names[0]):
        problems.append("the file as given passes its reference behaviour")
    indent = re.match(r"\s*", lines[line - 1]).group(0)
    fixed = [*lines[:line - 1], indent + answer["fix"], *lines[line:]]
    if not _passes("\n".join(fixed), names[0]):
        problems.append(f"line {line} with the fix applied does not pass the reference behaviour")
    return problems


def _selection(task: dict) -> str:
    text = " ".join(i["text"] for i in task["items"])
    found = {m.group(1): {"price": int(m.group(2)), "days": int(m.group(3)), "rating": int(m.group(4))}
             for m in re.finditer(r"([A-Z]\w+ \w+) quotes (\d+) per unit, delivers in (\d+) days, and holds a rating "
                                  r"of (\d+) out of 10\.", text)}
    for m in re.finditer(r"ignore the earlier quote from (.+?); its price is now (\d+) per unit", text):
        found[m.group(1)]["price"] = int(m.group(2))
    limits = re.fullmatch(r"Which supplier quotes at most (\d+) per unit, delivers within (\d+) days and is rated at "
                          r"least (\d+) out of 10\?", task["question"])
    price, days, rating = (int(g) for g in limits.groups())
    meeting = [n for n, s in found.items() if s["price"] <= price and s["days"] <= days and s["rating"] >= rating]
    if len(meeting) != 1:
        raise ValueError(f"{len(meeting)} suppliers meet the criteria")
    return meeting[0]


def replay(task: dict) -> list[str]:
    family, answer = task["family"], task["answer"]
    try:
        if family == "cq":
            return _code(task)
        if family == "dg":
            found = _digest(task)
            wanted = {k: v["expected"] for k, v in answer["fields"].items()}
            return [] if found == wanted else [f"the text says {found}, the answer {wanted}"]
        derived = {"em": _email, "dq": _document, "tq": _table, "sl": _selection}[family](task)
    except (AttributeError, KeyError, StopIteration, ValueError) as error:
        return [f"the text cannot be read: {error!r}"]
    return [] if derived == answer["expected"] else [f"the text says {derived}, the answer {answer['expected']}"]
