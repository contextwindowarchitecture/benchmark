"""Task families: seeded conversation scripts with ground truth known by construction (domain-2-plan.md, 6).

A script (schema kind `conversation`) is a whole conversation fixed in advance: the governance instructions, every
turn's user and assistant text, the shards (the sentences in a user turn that carry facts), the ground truth, and the
probes. A probe is a checkpoint: after turn t, its question is asked as the payload's query, with turns 1 to t as
history, and it never enters the history itself, so every probe of a conversation sees the same conversation. Each
probe carries what grading needs (its `answer`), the turns whose presence the fact-in-payload oracle checks
(`needs`), and the FULL and CONCAT controls' single-turn texts (domain-2-plan.md, 7).

A script names no run, so the same family, seed, turn count and parameters give the same bytes, and its blob digest
is stable across runs. A generator's VERSION moves when its output for the same inputs changes.
"""
from __future__ import annotations

import hashlib

from .. import output
from . import facts, variables

FAMILIES = {"vt": variables, "fr": facts}


def conversation_seed(family: str, seed: int, turns: int, index: int) -> int:
    """Each conversation's own seed, from the family's seed: adding conversations never changes the earlier ones."""
    return int(hashlib.sha256(f"{family}:{seed}:{turns}:{index}".encode()).hexdigest()[:12], 16)


def checkpoints(turns: int, every: int) -> list[int]:
    """The turns a probe follows: every `every` turns and the last turn."""
    points = list(range(every, turns + 1, every))
    return points if points and points[-1] == turns else [*points, turns]


def turn_id(turn: int) -> str:
    return f"t{turn:03d}"


def generate(family: str, seed: int, turns: int, index: int, every: int, parameters: dict) -> dict:
    """One script of `family`: its `index`-th conversation of `turns` turns."""
    module = FAMILIES[family]
    own_seed = conversation_seed(family, seed, turns, index)
    script = module.generate(own_seed, turns, checkpoints(turns, every), index, parameters)
    return {
        "$schema": output.schema_name("conversation"),
        "conversation_id": f"{family}-t{turns:03d}-{index:02d}",
        "family": family,
        "generator": {"name": module.GENERATOR, "version": module.VERSION},
        "seed": own_seed,
        "turn_count": turns,
        "checkpoint_every": every,
        "parameters": dict(sorted(parameters.items())),
        **script,
    }
