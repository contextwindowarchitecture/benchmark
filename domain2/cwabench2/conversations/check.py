"""The independent check of a script's ground truth (domain-2-plan.md, 5.1), for S0.

Ground truth is known by construction, so it is only as good as the generator. This module reads what a model would
read, the turns' text, and derives every probe's answer and `needs` again without the generator's code or its
internal state: from the numbers in each sentence and the phrases, values and keywords the script itself states. A
script whose text does not say what its ground truth says fails here.

`structure` checks the shape every family shares; `replay` the family's own facts. Each returns a list of problems,
empty when the script passes.
"""
from __future__ import annotations

import re

_DIGITS = re.compile(r"\d")
_NUMBER = re.compile(r"\d[\d,]*")  # how the scripts write numbers: digits, thousands separated by commas
_SENTENCES = re.compile(r"(?<=[.!?])\s+")
_DOUBLED = re.compile(r"\b(\w+) \1\b", re.IGNORECASE)  # "the the": a template filled with a word it already has


def _numbers(sentence: str) -> list[int]:
    return [int(m.group(0).replace(",", "")) for m in _NUMBER.finditer(sentence)]


def _sentences(text: str) -> list[str]:
    return [s for s in _SENTENCES.split(text) if s]


def _probe_points(turns: int, every: int) -> list[int]:
    points = list(range(every, turns + 1, every))
    return points if points and points[-1] == turns else [*points, turns]


def structure(script: dict) -> list[str]:
    problems = []
    turns = script["turns"]
    if [t["turn"] for t in turns] != list(range(1, script["turn_count"] + 1)):
        problems.append("turns are not numbered 1 to turn_count")
    if [t["id"] for t in turns] != [f"t{t['turn']:03d}" for t in turns]:
        problems.append("turn ids do not follow the turn numbers")
    expected = _probe_points(script["turn_count"], script["checkpoint_every"])
    if [p["after_turn"] for p in script["probes"]] != expected:
        problems.append(f"probes follow turns {[p['after_turn'] for p in script['probes']]}, not {expected}")
    for field in ("instructions", "output_contract"):
        if _DIGITS.search(script[field]):
            problems.append(f"the {field.replace('_', ' ')} contains a digit")
    for turn in turns:
        rest = turn["user"]
        for shard in turn["shards"]:
            if shard not in rest:
                problems.append(f"{turn['id']}: a shard is not in the user text")
            rest = rest.replace(shard, "", 1)
        if _DIGITS.search(rest):
            problems.append(f"{turn['id']}: a digit outside the shards")
        if _DIGITS.search(turn["assistant"]):
            problems.append(f"{turn['id']}: a digit in the assistant turn")
        if _DOUBLED.search(turn["user"]):
            problems.append(f"{turn['id']}: a doubled word")
    ids = {t["id"]: t["turn"] for t in turns}
    for probe in script["probes"]:
        if _DIGITS.search(probe["question"]):
            problems.append(f"{probe['probe_id']}: a digit in the question")
        late = [n for n in probe["needs"] if ids.get(n, probe["after_turn"] + 1) > probe["after_turn"]]
        if late:
            problems.append(f"{probe['probe_id']}: needs turns it has not reached: {late}")
    return problems


def _replay_variables(script: dict) -> list[str]:
    problems = []
    phrases = {v["phrase"].casefold(): v["variable_id"] for v in script["ground_truth"]["variables"]}
    found: dict[str, list[tuple[int, int]]] = {v: [] for v in phrases.values()}
    for turn in script["turns"]:
        for sentence in _sentences(turn["user"]):
            values = _numbers(sentence)
            named = [v for p, v in phrases.items() if re.search(rf"(?<!\w){re.escape(p)}(?!\w)", sentence.casefold())]
            if not values and not named:
                continue
            # A correction (CC) names the new value, then the retracted one: "… is 4,350, not 4,200."
            retracts = len(values) == 2 and ", not " in sentence
            if len(named) != 1 or len(values) != (2 if retracts else 1):
                problems.append(f"{turn['id']}: a sentence names {len(named)} variables and {len(values)} values")
                continue
            chain = found[named[0]]
            if retracts and (not chain or chain[-1][1] != values[1]):
                problems.append(f"{turn['id']}: a correction retracts {values[1]}, which is not the figure's value")
            found[named[0]].append((turn["turn"], values[0]))
    recorded = {v["variable_id"]: [(a["turn"], int(a["value"])) for a in v["assignments"]]
                for v in script["ground_truth"]["variables"]}
    if found != recorded:
        problems.append("the text's assignments differ from the ground truth's")
    if script["ground_truth"]["task"] == "corrections":
        for variable in script["ground_truth"]["variables"]:
            kinds = [a["kind"] for a in variable["assignments"]]
            if kinds[:1] != ["stated"] or any(k != "corrected" for k in kinds[1:]):
                problems.append(f"{variable['variable_id']}: not stated once and then only corrected")
        for turn in script["turns"]:
            for sentence in _sentences(turn["user"]):
                if ", not " in sentence and len(_numbers(sentence)) == 2:
                    continue
                if any(word in sentence for word in ("Update:", "Change ", "has been set", "From now on", "gone up",
                                                     "come down")):
                    problems.append(f"{turn['id']}: a reassignment in a corrections script")
    every_value = [value for chain in found.values() for _, value in chain]
    if len(every_value) != len(set(every_value)):
        problems.append("a value is assigned twice")
    for probe in script["probes"]:
        asked = [v for p, v in phrases.items() if p in probe["question"].casefold()]
        if len(asked) != 1:
            problems.append(f"{probe['probe_id']}: the question names {len(asked)} variables")
            continue
        after = probe["after_turn"]
        own = [(t, v) for t, v in found[asked[0]] if t <= after]
        if not own:
            problems.append(f"{probe['probe_id']}: asks a variable with no value yet")
            continue
        others = sorted(v for name, chain in found.items() if name != asked[0] for t, v in chain if t <= after)
        answer = probe["answer"]
        derived = {"kind": "number", "expected": str(own[-1][1]), "stale": [str(v) for _, v in own[:-1]],
                   "distractors": [str(v) for v in others]}
        if answer != derived:
            problems.append(f"{probe['probe_id']}: the answer differs from the text's")
        if probe["needs"] != [f"t{own[-1][0]:03d}"]:
            problems.append(f"{probe['probe_id']}: needs {probe['needs']}, the text says t{own[-1][0]:03d}")
    return problems


def _replay_record(script: dict) -> list[str]:
    problems = []
    turns = script["turns"]
    revealed = {}
    for field in script["ground_truth"]["fields"]:
        written = f"{int(field['value']):,}" if field["kind"] == "number" else field["value"]
        where = [t["turn"] for t in turns if re.search(rf"(?<![\w,]){re.escape(written)}(?![\w,]|,\d)", t["user"])]
        if where != [field["turn"]]:
            problems.append(f"{field['field']}: its value is written in turns {where}, not only {field['turn']}")
            continue
        if field["kind"] == "number" and _numbers(turns[field["turn"] - 1]["user"]) != [int(field["value"])]:
            problems.append(f"{field['field']}: turn {field['turn']} has other numbers than its value")
        revealed[field["field"]] = (field["turn"], field["value"], field["kind"])
    for probe in script["probes"]:
        after = probe["after_turn"]
        fields = {name: {"kind": kind, "expected": value if turn <= after else None}
                  for name, (turn, value, kind) in revealed.items()}
        if probe["answer"] != {"kind": "record", "fields": fields}:
            problems.append(f"{probe['probe_id']}: the answer differs from the text's")
        needs = [f"t{turn:03d}" for turn, _, _ in sorted(revealed.values()) if turn <= after]
        if probe["needs"] != needs:
            problems.append(f"{probe['probe_id']}: needs {probe['needs']}, the text says {needs}")
        missing = [name for name in revealed if not re.search(rf"\b{name}\b", probe["question"])]
        if missing:
            problems.append(f"{probe['probe_id']}: the question does not name {missing}")
    return problems


def _replay_compute(script: dict) -> list[str]:
    problems = []
    steps = []
    for turn in script["turns"]:
        for sentence in _sentences(turn["user"]):
            values = _numbers(sentence)
            if not values:
                continue
            if "Add a line of" in sentence and len(values) == 1:
                amount = values[0]
            elif "fee" in sentence and len(values) == 1:
                amount = values[0]
            elif " off " in sentence and len(values) == 1:
                amount = -values[0]
            else:
                problems.append(f"{turn['id']}: a sentence with numbers that is no order line")
                continue
            steps.append((turn["turn"], amount))
    recorded = [(s["turn"], s["amount"]) for s in script["ground_truth"]["steps"]]
    if steps != recorded:
        problems.append("the text's order lines differ from the ground truth's")
    totals, total = [], 0
    for turn, amount in steps:
        total += amount
        totals.append((turn, total))
    if str(total) != script["ground_truth"]["total"]:
        problems.append("the text's total differs from the ground truth's")
    for probe in script["probes"]:
        after = probe["after_turn"]
        known = [(t, v) for t, v in totals if t <= after]
        if not known:
            problems.append(f"{probe['probe_id']}: no order line before it")
            continue
        current = known[-1][1]
        derived = {"kind": "number", "marker": "Total:", "expected": str(current),
                   "stale": [str(v) for _, v in known[:-1] if v != current], "distractors": []}
        if probe["answer"] != derived:
            problems.append(f"{probe['probe_id']}: the answer differs from the text's")
        if probe["needs"] != [f"t{t:03d}" for t, _ in known]:
            problems.append(f"{probe['probe_id']}: needs differ from the text's order lines")
    return problems


REPLAYS = {"variables": _replay_variables, "corrections": _replay_variables, "record": _replay_record,
           "compute": _replay_compute}


def replay(script: dict) -> list[str]:
    return REPLAYS[script["ground_truth"]["task"]](script)
