"""Fictional vocabulary for the task generators (domain-5-plan.md, 6.1): people, things, places and the words canaries
are built from.

Nothing here is a real entity, so no model has seen the facts. Site names are Domain 2's onset-coda names
(cwabench2.conversations.names), so the two domains' entities look alike. Canary words are built from syllables no
task word contains, and S0 checks every canary of a run against every task text (domain-5-plan.md, 6.8).
"""
from __future__ import annotations

import random

from cwabench2.conversations.names import CODAS, ONSETS, capitalize, number

__all__ = ["CODAS", "ONSETS", "capitalize", "number"]

FIRST = ("Alder", "Brisa", "Corin", "Dalia", "Eamon", "Faye", "Gideon", "Halle", "Ivo", "Juno", "Kasimir", "Lark",
         "Maren", "Niall", "Odette", "Perrin", "Quilla", "Rowan", "Sabine", "Tamsin", "Ulric", "Vesna", "Wilder",
         "Ysolde")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
GOODS = (("crates", "lamp oil"), ("reams", "survey paper"), ("boxes", "brass fittings"), ("rolls", "sail canvas"),
         ("jars", "glazing compound"), ("bundles", "cedar shingles"), ("tins", "boot polish"),
         ("coils", "copper wire"), ("sacks", "seed barley"), ("cases", "signal flares"))
SITE_KINDS = ("observatory", "depot", "archive", "boathouse", "foundry", "conservatory", "granary", "mill")
PRODUCTS = ("lantern", "kettle", "compass", "ledger", "trowel", "satchel", "whistle", "thimble", "spindle", "bellows")
REGIONS = ("North", "South", "East", "West", "Coast", "Uplands")
CATERING = ("soup and bread", "a cold buffet", "tea and cakes", "a hot lunch", "sandwiches")
SUPPLIER_KINDS = ("Works", "Supply", "Traders", "Outfitters", "Cooperative")

# Canary words: three consonant-vowel syllables, upper case ("KAVORE"). No task word is built this way, and no
# syllable spells a common English word on its own.
SYLLABLES = ("KA", "VO", "RE", "TU", "MI", "ZA", "LO", "PE", "XI", "NU", "DE", "SO", "BI", "FA", "GU", "HE", "JO", "WI")


def site_names(rng: random.Random) -> list[str]:
    """Every onset-coda name, shuffled: draw from the front for distinct names."""
    pool = [onset + coda for onset in ONSETS for coda in CODAS]
    rng.shuffle(pool)
    return pool


def canary_word(rng: random.Random) -> str:
    return "".join(rng.choice(SYLLABLES) for _ in range(3))


def canary(rng: random.Random) -> tuple[str, str]:
    """Two distinct canary words; joined with a hyphen they are the canary (attacks/canaries.py)."""
    first = canary_word(rng)
    second = canary_word(rng)
    while second == first:
        second = canary_word(rng)
    return first, second
