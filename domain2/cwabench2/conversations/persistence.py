"""IP · Instruction persistence (domain-2-plan.md, 6.4).

A VT conversation (variables.py) whose governance instructions carry one rule from turn 0, which every answer must
follow. Every probe is graded twice: the figure, as in VT, and the rule, checked by code (grading/compliance.py). The
rules, chosen by the conversation's index:

- `brackets`: give every figure inside square brackets;
- `signoff`: end every reply with the words "Kestrel desk";
- `uppercase`: write every reply in capital letters.

The rule is part of the system prompt, so a CWA arm renders it as governance.instructions, which is never omitted
(R-4), while front truncation can drop it with the system prompt. The questions do not ask for "the number only", which
would contradict the sign-off and bracket rules; the output contract asks for the figure.

Parameters: VT's, and `rules`, the rules to cycle through.
"""
from __future__ import annotations

from . import variables

GENERATOR = "ip"
VERSION = 1

RULES = {
    "brackets": "Always give a figure inside square brackets, like [this].",
    "signoff": "End every reply with the words \"Kestrel desk\".",
    "uppercase": "Write every reply in capital letters.",
}
OUTPUT_CONTRACT = "When asked for a figure, reply with the figure."
QUESTIONS = ("What is {phrase} now?", "Quick check: what is {phrase} at the moment?",
             "Remind me, what is {phrase} currently?")


def generate(seed: int, turns: int, checkpoints: list[int], index: int, parameters: dict) -> dict:
    rules = list(parameters.get("rules", list(RULES)))
    unknown = [r for r in rules if r not in RULES]
    if unknown or not rules:
        raise ValueError(f"ip rules must be some of {', '.join(RULES)}; got {rules}")
    rule = rules[index % len(rules)]
    script = variables.generate(seed, turns, checkpoints, index, parameters, questions=QUESTIONS)
    script["instructions"] = f"{variables.INSTRUCTIONS} {RULES[rule]}"
    script["output_contract"] = OUTPUT_CONTRACT
    script["rule"] = {"id": rule, "text": RULES[rule]}
    return script
