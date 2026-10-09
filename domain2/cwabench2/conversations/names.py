"""Fictional vocabulary for the conversation generators (domain-2-plan.md, 6): names, places, filler and replies.

Nothing here is a real entity, so no model has seen the facts. Two rules keep the ground truth exact, and S0 checks
both on every script: filler, replies and instructions contain no digits, so every number in a user turn is a fact;
and filler draws its names from pools (people, places, parts of the day) that no fact uses.
"""
from __future__ import annotations

import random

ONSETS = ("Har", "Bren", "Cal", "Dov", "Ell", "Fen", "Gar", "Hol", "Ist", "Jor", "Kel", "Lun", "Mor", "Nev", "Orl",
          "Pell", "Quin", "Ros", "Sel", "Tor", "Ulm", "Var", "Wyn", "Yor", "Zel")
CODAS = ("well", "nick", "more", "ton", "by", "worth", "ley", "stead", "wick", "dale", "mere", "croft", "hurst", "ford",
         "bury")

# Filler only. None of these is ever a fact, and none can be built from ONSETS and CODAS.
PEOPLE = ("Ada", "Bram", "Cora", "Dell", "Esme", "Finn", "Greta", "Hugo", "Ines", "Jonah", "Kira", "Leon", "Mira", "Nils",
          "Opal", "Pia", "Rafe", "Suri", "Teo", "Una", "Vik", "Wren")
PLACES = ("east wing", "north office", "harbour room", "library annex", "garden studio", "print room", "loading bay")
PARTS_OF_DAY = ("morning", "afternoon", "evening")

FILLER = (
    "{person} asked whether the {place} is free in the {part}.",
    "Remind me later that {person} prefers the {place} for reviews.",
    "The {place} is being repainted, so expect some noise in the {part}.",
    "{person} and {other} are swapping desks this week.",
    "I still need to send {person} the notes from the {part} meeting.",
    "The coffee machine in the {place} is working again.",
    "{person} thinks the {place} is too cold in the {part}.",
    "We moved the {part} stand-up to the {place}.",
    "{person} is out for the rest of the week, so {other} is covering.",
    "Someone left an umbrella in the {place} again.",
    "{person} wants the {place} kept clear for the visitors.",
    "The lift near the {place} is slow today.",
)

REPLIES = ("Noted.", "Got it.", "Understood.", "Thanks, noted.", "All right, I have that.",
           "Okay, I'll keep that in mind.")


def names(rng: random.Random) -> list[str]:
    """Every onset-coda name, shuffled: draw from the front to get distinct names."""
    pool = [onset + coda for onset in ONSETS for coda in CODAS]
    rng.shuffle(pool)
    return pool


def similar(name: str, taken: set[str], rng: random.Random) -> str:
    """A name sharing `name`'s onset with another coda, not yet taken: the near-miss a distractor needs."""
    onset = next(o for o in sorted(ONSETS, key=len, reverse=True) if name.startswith(o))
    choices = [onset + coda for coda in CODAS if onset + coda != name and onset + coda not in taken]
    return rng.choice(choices)


def filler(rng: random.Random) -> str:
    person, other = rng.sample(PEOPLE, 2)
    return rng.choice(FILLER).format(person=person, other=other, place=rng.choice(PLACES),
                                     part=rng.choice(PARTS_OF_DAY))


def reply(rng: random.Random) -> str:
    return rng.choice(REPLIES)


def number(n: int) -> str:
    """A number as the scripts write it: thousands separated by commas."""
    return f"{n:,}"


def capitalize(sentence: str) -> str:
    return sentence[:1].upper() + sentence[1:]


def user_text(shards: list[str], filler_count: int, rng: random.Random) -> str:
    """A user turn: its shards (the sentences that carry facts) among filler sentences, at a seeded position. A turn
    with no shard still says something."""
    sentences = [filler(rng) for _ in range(max(filler_count, 0 if shards else 1))]
    for shard in shards:
        sentences.insert(rng.randrange(len(sentences) + 1), shard)
    return " ".join(sentences)
