"""Mutation fuzzing (domain-1-plan.md, 7.5): a valid snapshot broken in exactly one snapshot check or schema rule.

Every assembler must reject a mutant before assembly (exit 2), and none may crash or hang. Each operator names the check
it breaks (canon/validity.py), and a mutant is kept only when the validity checker finds that check, and only that one,
broken, so the expected answer does not rest on the operator being right.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from typing import Callable

from ...canon import validity
from ...contract import Contract


@dataclass(frozen=True)
class Operator:
    name: str
    check: str  # the validity check it breaks
    apply: Callable[[dict, random.Random], bytes | None]  # None: not applicable to this snapshot


def _dump(document: dict, ascii_only: bool = False) -> bytes:
    return json.dumps(document, ensure_ascii=ascii_only, separators=(",", ":")).encode("utf-8")


def _items(document: dict) -> list[dict]:
    return [i for b in document["batches"] for i in b["items"] if isinstance(i, dict)]


def _ids(document: dict) -> list[str]:
    return [i["id"] for i in _items(document) if isinstance(i.get("id"), str) and i["id"].strip()]


# I-JSON ----------------------------------------------------------------------------------------------------------------


def _surrogate(document, rng):
    items = [i for i in _items(document) if isinstance(i.get("body"), str)]
    rng.choice(items)["body"] += rng.choice(("\ud800", "\udfff", "\udc00x"))
    return _dump(document, ascii_only=True)  # json writes the lone surrogate as a \u escape: valid JSON, not I-JSON


def _big_number(literal):  # also NaN and Infinity, which are not JSON at all
    def apply(document, rng):
        rng.choice(_items(document))["relevance"] = "@@NUMBER@@"
        return _dump(document).replace(b'"@@NUMBER@@"', literal.encode())
    return apply


def _invalid_utf8(document, rng):
    item = rng.choice([i for i in _items(document) if isinstance(i.get("body"), str)])
    item["body"] += "@@BYTES@@"
    return _dump(document).replace(b"@@BYTES@@", rng.choice((b"\xff", b"\xc3\x28", b"\xed\xa0\x80")))


# Batches, conflicts and producer exclusions ---------------------------------------------------------------------------


def _producer_twice(document, rng):
    batch = rng.choice(document["batches"])
    if len(batch["items"]) >= 2 and rng.random() < 0.7:
        half = len(batch["items"]) // 2
        twin = {"producer": dict(batch["producer"]), "items": batch["items"][half:], "excluded": []}
        batch["items"] = batch["items"][:half]
    else:
        twin = {"producer": dict(batch["producer"]), "items": [], "excluded": []}
    document["batches"].insert(rng.randrange(len(document["batches"]) + 1), twin)
    return _dump(document)


def _named(document) -> set[str]:
    return {i for g in document["conflicts"] for i in g["items"]}


def _free_ids(document, n: int, rng) -> list[str] | None:
    free = sorted(set(_ids(document)) - _named(document))
    return rng.sample(free, n) if len(free) >= n else None


def _group_unknown_item(document, rng):
    ids = _free_ids(document, 1, rng)
    if ids is None:
        return None
    document["conflicts"].append({"id": "g-mut", "kind": "instruction", "items": [ids[0], "no-such-item"]})
    return _dump(document)


def _group_overlap(document, rng):
    ids = _free_ids(document, 3, rng)
    if ids is None:
        return None
    document["conflicts"] += [{"id": "g-mut1", "kind": "instruction", "items": ids[:2]},
                              {"id": "g-mut2", "kind": "instruction", "items": ids[1:]}]
    return _dump(document)


def _group_repeated_id(document, rng):
    ids = _free_ids(document, 4, rng)
    if ids is None:
        return None
    document["conflicts"] += [{"id": "g-mut", "kind": "instruction", "items": ids[:2]},
                              {"id": "g-mut", "kind": "instruction", "items": ids[2:]}]
    return _dump(document)


def _group_unknown_fact(document, rng):
    ids = _free_ids(document, 2, rng)
    if ids is None:
        return None
    document["conflicts"].append({"id": "g-mut", "kind": "fact", "fact": "no-such-fact", "items": ids})
    return _dump(document)


def _exclusion_ref(ref):
    def apply(document, rng):
        batch = rng.choice(document["batches"])
        reason = "duplicate_content" if ref == "duplicate_of" else "superseded"
        batch["excluded"].append({"item_id": "mut:row", "reason": reason, "stage": "producer", ref: "no-such-item"})
        return _dump(document)
    return apply


# Profile ---------------------------------------------------------------------------------------------------------------


def _profile_field(field, value):
    def apply(document, rng):
        document["profile"][field] = value
        return _dump(document)
    return apply


def _unplace(slot):
    def apply(document, rng):
        document["profile"]["placement"] = [p for p in document["profile"]["placement"] if p["slot"] != slot]
        if len(document["profile"]["placement"]) < 2:
            return None  # the schema's minItems would break too
        return _dump(document)
    return apply


def _parser_unplaced(document, rng):
    document["route_policy"]["parser"] = True
    document["profile"]["placement"] = [p for p in document["profile"]["placement"]
                                        if p["slot"] != "governance.output_contract"]
    return _dump(document)


# Realizability ----------------------------------------------------------------------------------------------------------


def _xml_renders_system(document, rng):
    document["renderer"] = "fixture-xml/v1"
    for p in document["profile"]["placement"]:
        if p["wrap"] in ("system", "tools"):
            p["wrap"] = "xml:restored"
    governance = [p for p in document["profile"]["placement"] if p["slot"].startswith("governance.")]
    rng.choice(governance)["wrap"] = rng.choice(("system", "tools"))
    return _dump(document)


def _invalid_tag(document, rng):
    placement = [p for p in document["profile"]["placement"] if p["wrap"].startswith("xml:")]
    rng.choice(placement)["wrap"] = rng.choice(("xml:1bad", "xml:", "xml:has space", "xml:é", "html:div"))
    return _dump(document)


def _messages(mutation):
    def apply(document, rng):
        document["renderer"] = rng.choice(("cwa-messages/v1", "cwa-message-blocks/v1"))
        placements = document["profile"]["placement"]
        if mutation == "system-on-non-governance":
            candidates = [p for p in placements if not p["slot"].startswith("governance.")]
            rng.choice(candidates)["wrap"] = "system"
            # keep every system placement first, so only this check breaks
            placements.sort(key=lambda p: p["wrap"] != "system")
        elif mutation == "tools-on-non-capabilities":
            candidates = [p for p in placements if p["slot"] != "governance.capabilities" and p["wrap"] != "system"]
            rng.choice(candidates)["wrap"] = "tools"
        elif mutation == "system-after-xml":
            governance = [p for p in placements if p["slot"].startswith("governance.")
                          and p["slot"] != "governance.capabilities"]
            moved = rng.choice(governance)
            placements.remove(moved)
            placements.append({"slot": moved["slot"], "wrap": "system"})
            if not any(p["wrap"].startswith("xml:") for p in placements[:-1]):
                return None
        return _dump(document)
    return apply


# Schemas ----------------------------------------------------------------------------------------------------------------


def _drop(path):
    def apply(document, rng):
        *parents, last = path
        target = document
        for key in parents:
            target = target[key]
        target.pop(last, None)
        return _dump(document)
    return apply


def _set(path, value):
    def apply(document, rng):
        *parents, last = path
        target = document
        for key in parents:
            target = target[key]
        target[last] = value
        return _dump(document)
    return apply


def _entry_not_object(document, rng):
    rng.choice(document["batches"])["items"].insert(0, rng.choice((None, 7, "item", [])))
    return _dump(document)


def _unknown_producer_reason(document, rng):
    rng.choice(document["batches"])["excluded"].append({"item_id": "mut:row", "reason": "rate_limited",
                                                        "stage": "producer"})
    return _dump(document)


OPERATORS = [
    Operator("json:nan-literal", "json", _big_number("NaN")),
    Operator("json:infinity-literal", "json", _big_number("-Infinity")),
    Operator("json:invalid-utf8", "json", _invalid_utf8),
    Operator("i-json:unpaired-surrogate", "i-json", _surrogate),
    Operator("i-json:1e400", "i-json", _big_number("1e400")),
    Operator("i-json:-1e400", "i-json", _big_number("-1e400")),
    Operator("i-json:400-digit-integer", "i-json", _big_number("1" + "0" * 400)),
    Operator("batches:producer-twice", "one-batch-per-producer", _producer_twice),
    Operator("conflicts:unknown-item", "conflict-groups", _group_unknown_item),
    Operator("conflicts:overlap", "conflict-groups", _group_overlap),
    Operator("conflicts:repeated-id", "conflict-groups", _group_repeated_id),
    Operator("conflicts:unknown-fact", "conflict-groups", _group_unknown_fact),
    Operator("exclusions:duplicate-of-unknown", "producer-exclusions", _exclusion_ref("duplicate_of")),
    Operator("exclusions:superseded-by-unknown", "producer-exclusions", _exclusion_ref("superseded_by")),
    Operator("profile:route-mismatch", "profile", _profile_field("route", "another-route")),
    Operator("profile:route-policy-version", "profile", _profile_field("route_policy_version", "bench/other")),
    Operator("profile:missing-instructions", "profile", _unplace("governance.instructions")),
    Operator("profile:missing-query", "profile", _unplace("interaction.query")),
    Operator("profile:parser-without-output-contract", "profile", _parser_unplaced),
    Operator("realizable:xml-platform-wrap", "realizable", _xml_renders_system),
    Operator("realizable:invalid-tag", "realizable", _invalid_tag),
    Operator("realizable:system-on-non-governance", "realizable", _messages("system-on-non-governance")),
    Operator("realizable:tools-on-non-capabilities", "realizable", _messages("tools-on-non-capabilities")),
    Operator("realizable:system-after-xml", "realizable", _messages("system-after-xml")),
    Operator("schema:missing-budget", "schema", _drop(("budget",))),
    Operator("schema:missing-conflicts", "schema", _drop(("conflicts",))),
    Operator("schema:profile-spec", "schema", _profile_field("spec", "cwa/1")),
    Operator("schema:negative-budget", "schema", _set(("budget", "input"), -1)),
    Operator("schema:fractional-budget", "schema", _set(("budget", "input"), 100.5)),
    Operator("schema:impossible-assembly-time", "schema", _set(("assembly_time",), "2026-02-30T12:00:00Z")),
    Operator("schema:leap-second", "schema", _set(("assembly_time",), "2026-06-30T23:59:60Z")),
    Operator("schema:extra-member", "schema", _set(("extensions",), {})),
    Operator("schema:entry-not-object", "schema", _entry_not_object),
    Operator("schema:unknown-producer-reason", "schema", _unknown_producer_reason),
    Operator("schema:unknown-producer-kind", "schema", lambda d, r: (r.choice(d["batches"])["producer"].update(
        kind="search"), _dump(d))[1]),
    Operator("schema:blank-tokenizer", "schema", _set(("tokenizer",), "﻿")),
]
BY_NAME = {o.name: o for o in OPERATORS}


@dataclass(frozen=True)
class Mutant:
    case_id: str
    data: bytes
    operator: str
    check: str
    parent: str  # the valid snapshot's case id


def mutate(contract: Contract, document: dict, parent: str, rng: random.Random, operator: Operator,
           case_id: str) -> Mutant | None:
    """Apply one operator; None when it does not apply or would break more than its own check."""
    data = operator.apply(json.loads(json.dumps(document)), rng)
    if data is None or validity.broken_checks(contract, data) != [operator.check]:
        return None
    return Mutant(case_id, data, operator.name, operator.check, parent)
