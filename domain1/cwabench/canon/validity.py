"""Whether a snapshot is valid, and which check each problem breaks (conformance/README.md, Snapshot checks).

An invalid snapshot is rejected before assembly (R-17). The fuzzer builds valid snapshots and mutants that break exactly
one check, and the minimizer keeps a reduced snapshot valid, so all three need to know which check a problem belongs to.
Every published rejection breaks exactly one of these, which tests/test_validity.py confirms.

    problems(contract, data) -> [(check, message), ...]     empty when the snapshot is valid
"""
from __future__ import annotations

import json
import math

from .payloads import TAG

# Check ids, in the order they are applied. "json" and "schema" are the snapshot's schemas; the rest are the README's
# Snapshot checks, with the profile's realizability split out because it depends on the renderer.
CHECKS = ("json", "i-json", "schema", "one-batch-per-producer", "conflict-groups", "producer-exclusions", "profile",
          "realizable")
REQUIRED_PLACEMENTS = ("governance.instructions", "interaction.query")
MESSAGE_RENDERERS = ("cwa-messages/v1", "cwa-message-blocks/v1")


class _Numbers:
    """Collects numbers outside the double range while json.loads reads them."""

    def __init__(self):
        self.out_of_range: list[str] = []

    def integer(self, text: str):
        try:
            value = int(text)
            float(value)
        except (ValueError, OverflowError):  # longer than Python's digit limit, or beyond the double range
            self.out_of_range.append(text[:40])
            return 0
        return value

    def real(self, text: str):
        value = float(text)
        if math.isinf(value):
            self.out_of_range.append(text[:40])
            return 0.0
        return value


def _constant(name: str):
    raise ValueError(f"{name} is not JSON")


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from _strings(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _strings(inner)


def _lone_surrogate(text: str) -> bool:
    return any("\ud800" <= c <= "\udfff" for c in text)


def load(data: bytes) -> tuple[object, list[tuple[str, str]]]:
    """The document, and its JSON and I-JSON problems."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        return None, [("json", f"not UTF-8: {error}")]
    numbers = _Numbers()
    try:
        document = json.loads(text, parse_int=numbers.integer, parse_float=numbers.real, parse_constant=_constant)
    except (json.JSONDecodeError, ValueError) as error:
        return None, [("json", f"not a JSON document: {error}")]
    found = [("i-json", f"number {n} is beyond the IEEE 754 double range") for n in numbers.out_of_range]
    if any(_lone_surrogate(s) for s in _strings(document)):
        found.append(("i-json", "a string holds an unpaired surrogate"))
    return document, found


def _candidate_ids(batch: dict) -> set[str]:
    return {i["id"] for i in batch.get("items", []) if isinstance(i, dict) and isinstance(i.get("id"), str)}


def _realizable(renderer, placements: list[dict]) -> list[str]:
    out = []
    if renderer == "fixture-xml/v1":
        for p in placements:
            wrap = p["wrap"]
            if not (wrap.startswith("xml:") and TAG.fullmatch(wrap[4:])):
                out.append(f"fixture-xml/v1 cannot render wrap {wrap!r}")
        return out
    if renderer not in MESSAGE_RENDERERS:
        return out  # not a published renderer: unsupported, which is no problem with the snapshot
    seen_xml = False
    for p in placements:
        wrap, slot = p["wrap"], p["slot"]
        if wrap == "system":
            if not slot.startswith("governance."):
                out.append(f"{slot} placed in system, which only governance slots may take")
            if seen_xml:
                out.append(f"a system placement of {slot} follows an xml: placement")
        elif wrap == "tools":
            if slot != "governance.capabilities":
                out.append(f"{slot} placed in tools, which only governance.capabilities may take")
        elif wrap.startswith("xml:") and TAG.fullmatch(wrap[4:]):
            seen_xml = True
        else:
            out.append(f"{renderer} cannot render wrap {wrap!r}")
    return out


def problems(contract, data: bytes) -> list[tuple[str, str]]:
    document, found = load(data)
    if document is None:
        return found
    errors = sorted(contract.validator("snapshot.schema.json").iter_errors(document), key=lambda e: list(e.absolute_path))
    if errors:
        where = "/" + "/".join(str(p) for p in errors[0].absolute_path)
        found.append(("schema", f"{where}: {errors[0].message[:200]}"))
        return found  # the checks below read the snapshot's structure, which the schema has not confirmed

    batches = document["batches"]
    producers = [b["producer"]["id"] for b in batches]
    repeated = sorted({p for p in producers if producers.count(p) > 1})
    if repeated:
        found.append(("one-batch-per-producer", f"producers in several batches: {repeated}"))

    known = set()
    for batch in batches:
        known |= _candidate_ids(batch)
        known |= {row["item_id"] for row in batch["excluded"]}
    group_ids = [g["id"] for g in document["conflicts"]]
    if len(group_ids) != len(set(group_ids)):
        found.append(("conflict-groups", "group ids repeat"))
    member_of: dict[str, str] = {}
    facts = document["route_policy"].get("facts") or {}
    for group in document["conflicts"]:
        for item_id in group["items"]:
            if item_id not in known:
                found.append(("conflict-groups", f"group {group['id']!r} names unknown item {item_id!r}"))
            if item_id in member_of:
                found.append(("conflict-groups", f"{item_id!r} is in groups {member_of[item_id]!r} and {group['id']!r}"))
            member_of[item_id] = group["id"]
        if group["kind"] == "fact" and group.get("fact") not in facts:
            found.append(("conflict-groups", f"group {group['id']!r} names fact {group.get('fact')!r}, not in the route"))

    for batch in batches:
        ids = _candidate_ids(batch)
        for row in batch["excluded"]:
            for ref in ("duplicate_of", "superseded_by"):
                if ref in row and row[ref] not in ids:
                    found.append(("producer-exclusions", f"{row['item_id']!r} {ref} {row[ref]!r} is no candidate in "
                                                         f"batch {batch['producer']['id']!r}"))

    profile, route = document["profile"], document["route_policy"]
    if profile["route"] != route["route"]:
        found.append(("profile", f"profile route {profile['route']!r} is not {route['route']!r}"))
    if profile["route_policy_version"] != route["version"]:
        found.append(("profile", f"profile expects {profile['route_policy_version']!r}, route is {route['version']!r}"))
    placed = {p["slot"] for p in profile["placement"]}
    needed = REQUIRED_PLACEMENTS + (("governance.output_contract",) if route.get("parser") is True else ())
    for slot in needed:
        if slot not in placed:
            found.append(("profile", f"the profile does not place {slot}"))
    for message in _realizable(document["renderer"], profile["placement"]):
        found.append(("realizable", message))
    return found


def broken_checks(contract, data: bytes) -> list[str]:
    """The distinct checks a snapshot breaks, in CHECKS order."""
    seen = {check for check, _ in problems(contract, data)}
    return [c for c in CHECKS if c in seen]


def is_valid(contract, data: bytes) -> bool:
    return not problems(contract, data)


__all__ = ["CHECKS", "broken_checks", "is_valid", "load", "problems"]
