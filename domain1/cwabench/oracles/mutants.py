"""Mutation operators for testing the auditor (domain-1-plan.md, section 11).

Each operator takes a known-correct (trace, payload) pair and returns small, specific corruptions of it, the kind an
assembler bug would produce. The auditor's kill rate is the share of mutants it reports. Some operators keep the
mutant internally consistent (rehashing the payload, recounting tokens) so only deeper checks can catch it.
"""
from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass
from typing import Callable, Iterator

from ..canon.payloads import Parsed, ParseError, parse
from ..canon.tokenizers import TOKENIZERS

SLOT_ROTATION = ("governance.instructions", "evidence.knowledge", "interaction.history", "state.task")
DECIDED = ("authority", "policy", "freshness", "escalated", "moot")
RESOLUTIONS = ("resolved", "surfaced", "context_requested", "refused", "moot")
ACTIONS = ("request_context", "precompute_summary", "retrieve_narrower")


@dataclass(frozen=True)
class Mutant:
    operator: str
    position: str  # where it applied, for people reading the report
    trace: dict
    payload: bytes | None


def _ends(rows: list) -> list[int]:
    return sorted({0, len(rows) - 1}) if rows else []


def _rehash(trace: dict, payload: bytes) -> None:
    if isinstance(trace.get("result"), dict):
        trace["result"]["hash"] = hashlib.sha256(payload).hexdigest()


def _next(values: tuple, current) -> object:
    for value in values[values.index(current) + 1:] + values[:values.index(current)] if current in values else values:
        if value != current:
            return value
    return current


class Mutator:
    def __init__(self, contract, snapshot: dict):
        self.contract = contract
        self.snapshot = snapshot
        self.exclusion_codes = tuple(r["code"] for r in contract.reasons if r["kind"] == "exclusion"
                                     and "<" not in r["code"])
        self.refusal_codes = tuple(r["code"] for r in contract.reasons if r["kind"] == "refusal")

    def operators(self) -> dict[str, Callable[[dict, bytes | None], Iterator[tuple[str, dict, bytes | None]]]]:
        return {name[3:]: getattr(self, name) for name in sorted(dir(self)) if name.startswith("op_")}

    def mutants(self, trace: dict, payload: bytes | None) -> list[Mutant]:
        out = []
        for name, operator in self.operators().items():
            for position, mutated, new_payload in operator(trace, payload):
                if mutated != trace or new_payload != payload:  # an equivalent mutant tests nothing
                    out.append(Mutant(name, position, mutated, new_payload))
        return out

    # Excluded rows ----------------------------------------------------------------------------------------------

    def op_drop_excluded_row(self, trace, payload):
        for i in _ends(trace["excluded"]):
            t = copy.deepcopy(trace)
            del t["excluded"][i]
            yield f"excluded/{i}", t, payload

    def op_duplicate_excluded_row(self, trace, payload):
        for i in _ends(trace["excluded"])[:1]:
            t = copy.deepcopy(trace)
            t["excluded"].insert(i, copy.deepcopy(t["excluded"][i]))
            yield f"excluded/{i}", t, payload

    def op_swap_excluded_rows(self, trace, payload):
        rows = trace["excluded"]
        pairs = [i for i in range(len(rows) - 1) if rows[i] != rows[i + 1]]
        for i in sorted({pairs[0], pairs[-1]}) if pairs else []:
            t = copy.deepcopy(trace)
            t["excluded"][i], t["excluded"][i + 1] = t["excluded"][i + 1], t["excluded"][i]
            yield f"excluded/{i}<->{i + 1}", t, payload

    def op_change_reason(self, trace, payload):
        for i in _ends(trace["excluded"]):
            t = copy.deepcopy(trace)
            row = t["excluded"][i]
            current = row["reason"].split(":")[0] if row["reason"].startswith("missing_field:") else row["reason"]
            row["reason"] = _next(self.exclusion_codes, current)
            row.pop("duplicate_of", None)
            row.pop("superseded_by", None)
            yield f"excluded/{i}/reason", t, payload

    def op_change_excluded_slot(self, trace, payload):
        for i, row in enumerate(trace["excluded"]):
            if "slot" in row:
                t = copy.deepcopy(trace)
                t["excluded"][i]["slot"] = _next(SLOT_ROTATION, row["slot"])
                yield f"excluded/{i}/slot", t, payload
                return

    def op_retarget_duplicate_or_superseded(self, trace, payload):
        others = [r["item_id"] for r in trace["included"]]
        for i, row in enumerate(trace["excluded"]):
            for key in ("duplicate_of", "superseded_by"):
                if key in row and row["stage"] == "assembler":
                    target = next((o for o in others if o != row[key]), row["item_id"])
                    t = copy.deepcopy(trace)
                    t["excluded"][i][key] = target
                    yield f"excluded/{i}/{key}", t, payload

    def op_ghost_excluded_row(self, trace, payload):
        t = copy.deepcopy(trace)
        t["excluded"].append({"item_id": "ghost:no-such-item", "reason": "over_budget", "stage": "assembler",
                              "slot": "evidence.knowledge"})
        yield "excluded/+ghost", t, payload

    # Included rows ----------------------------------------------------------------------------------------------

    def op_drop_included_row(self, trace, payload):
        for i in _ends(trace["included"]):
            t = copy.deepcopy(trace)
            del t["included"][i]
            yield f"included/{i}", t, payload

    def op_swap_included_rows(self, trace, payload):
        rows = trace["included"]
        pairs = [i for i in range(len(rows) - 1) if rows[i] != rows[i + 1]]
        for i in pairs[:1]:
            t = copy.deepcopy(trace)
            t["included"][i], t["included"][i + 1] = t["included"][i + 1], t["included"][i]
            yield f"included/{i}<->{i + 1}", t, payload

    def op_included_tokens(self, trace, payload):
        for i in _ends(trace["included"])[:1]:
            t = copy.deepcopy(trace)
            t["included"][i]["tokens"] += 1
            yield f"included/{i}/tokens", t, payload

    def op_included_source_version(self, trace, payload):
        for i in _ends(trace["included"])[:1]:
            t = copy.deepcopy(trace)
            t["included"][i]["source_version"] += "-mutated"
            yield f"included/{i}/source_version", t, payload

    def op_included_eligibility(self, trace, payload):
        for i in _ends(trace["included"])[:1]:
            t = copy.deepcopy(trace)
            t["included"][i]["eligibility"] += " (mutated)"
            yield f"included/{i}/eligibility", t, payload

    def op_move_included_to_excluded(self, trace, payload):
        for i in _ends(trace["included"]):
            t = copy.deepcopy(trace)
            row = t["included"].pop(i)
            t["excluded"].append({"item_id": row["item_id"], "reason": "over_budget", "stage": "assembler",
                                  "slot": row["slot"]})
            yield f"included/{i}->excluded", t, payload

    def op_revive_droppable(self, trace, payload):
        """Keep a droppable item that fitting omitted, as an assembler that ignores tier order would."""
        from .auditor.model import View  # the auditor's view of tiers, not its checks

        view = View.build(self.contract, self.snapshot)
        for i, row in enumerate(trace["excluded"]):
            candidate = view.unique(row["item_id"]) if row.get("reason") == "over_budget" else None
            if candidate is not None and view.tier(candidate) == "droppable":
                t = copy.deepcopy(trace)
                del t["excluded"][i]
                t["included"].append({"slot": candidate.slot, "item_id": candidate.recorded_id, "tokens": 1,
                                      "source_version": candidate.get("source_version"),
                                      "eligibility": view.filled(candidate, "eligibility")})
                yield f"excluded/{i}->included", t, payload
                return

    # Compressed rows ----------------------------------------------------------------------------------------------

    def op_compressed_variant(self, trace, payload):
        for i in _ends(trace["compressed"])[:1]:
            t = copy.deepcopy(trace)
            t["compressed"][i]["variant_id"] += "-mutated"
            yield f"compressed/{i}/variant_id", t, payload

    def op_compressed_counts(self, trace, payload):
        for i in _ends(trace["compressed"])[:1]:
            for key in ("from", "to"):
                t = copy.deepcopy(trace)
                t["compressed"][i][key] += 1
                yield f"compressed/{i}/{key}", t, payload

    def op_drop_compressed_row(self, trace, payload):
        for i in _ends(trace["compressed"])[:1]:
            t = copy.deepcopy(trace)
            del t["compressed"][i]
            yield f"compressed/{i}", t, payload

    # Result, context, budget, profile ------------------------------------------------------------------------------

    def op_input_tokens(self, trace, payload):
        if isinstance(trace.get("result"), dict):
            t = copy.deepcopy(trace)
            t["result"]["input_tokens"] += 1
            yield "result/input_tokens", t, payload

    def op_hash(self, trace, payload):
        if isinstance(trace.get("result"), dict):
            t = copy.deepcopy(trace)
            h = t["result"]["hash"]
            t["result"]["hash"] = ("0" if h[0] != "0" else "1") + h[1:]
            yield "result/hash", t, payload

    def op_snapshot_digest(self, trace, payload):
        t = copy.deepcopy(trace)
        d = t["context"]["snapshot_digest"]
        t["context"]["snapshot_digest"] = ("0" if d[0] != "0" else "1") + d[1:]
        yield "context/snapshot_digest", t, payload

    def op_context_echo(self, trace, payload):
        for key, value in (("assembly_time", "2001-01-01T00:00:00Z"), ("tokenizer", "estimate-utf8/v1"
                           if trace["context"]["tokenizer"] != "estimate-utf8/v1" else "fixture-whitespace/v1")):
            t = copy.deepcopy(trace)
            t["context"][key] = value
            yield f"context/{key}", t, payload

    def op_budget(self, trace, payload):
        t = copy.deepcopy(trace)
        t["budget"]["input"] += 1
        yield "budget/input", t, payload
        t = copy.deepcopy(trace)
        if "margin_percent" in t["budget"]:
            del t["budget"]["margin_percent"]
        else:
            t["budget"]["margin_percent"] = 10
        yield "budget/margin_percent", t, payload

    def op_profile_version(self, trace, payload):
        t = copy.deepcopy(trace)
        t["profile"]["version"] += 1
        yield "profile/version", t, payload

    # Refusal and recovery ---------------------------------------------------------------------------------------

    def op_refused_flip(self, trace, payload):
        t = copy.deepcopy(trace)
        t["refused"]["bool"] = not t["refused"]["bool"]
        yield "refused/bool", t, payload

    def op_refusal_reason(self, trace, payload):
        if trace["refused"]["bool"]:
            t = copy.deepcopy(trace)
            t["refused"]["reason"] = _next(self.refusal_codes, t["refused"]["reason"])
            yield "refused/reason", t, payload

    def op_recovery(self, trace, payload):
        recovery = trace.get("recovery")
        if isinstance(recovery, dict):
            t = copy.deepcopy(trace)
            t["recovery"]["action"] = _next(ACTIONS, recovery.get("action"))
            yield "recovery/action", t, payload
            t = copy.deepcopy(trace)
            del t["recovery"]
            yield "recovery", t, payload
        else:
            t = copy.deepcopy(trace)
            t["recovery"] = {"action": "request_context"}
            yield "recovery/+", t, payload

    # Conflicts and defaults ---------------------------------------------------------------------------------------

    def op_conflict_record(self, trace, payload):
        for i in _ends(trace["conflicts"])[:1]:
            record = trace["conflicts"][i]
            t = copy.deepcopy(trace)
            del t["conflicts"][i]
            yield f"conflicts/{i}", t, payload
            t = copy.deepcopy(trace)
            t["conflicts"][i]["decided_by"] = _next(DECIDED, record["decided_by"])
            yield f"conflicts/{i}/decided_by", t, payload
            t = copy.deepcopy(trace)
            t["conflicts"][i]["resolution"] = _next(RESOLUTIONS, record["resolution"])
            yield f"conflicts/{i}/resolution", t, payload
            if "winner" in record:
                other = next((x for x in record["items"] if x != record["winner"]), None)
                t = copy.deepcopy(trace)
                if other:
                    t["conflicts"][i]["winner"] = other
                else:
                    del t["conflicts"][i]["winner"]
                yield f"conflicts/{i}/winner", t, payload

    def op_defaults_filled(self, trace, payload):
        rows = trace["defaults_filled"]
        for i in _ends(rows):
            t = copy.deepcopy(trace)
            del t["defaults_filled"][i]
            yield f"defaults_filled/{i}", t, payload
        if rows:
            t = copy.deepcopy(trace)
            t["defaults_filled"].append(copy.deepcopy(rows[0]))
            yield "defaults_filled/+dup", t, payload

    # Payload ----------------------------------------------------------------------------------------------------

    def op_payload_byte(self, trace, payload):
        if payload:
            yield "payload/last-byte", copy.deepcopy(trace), payload[:-1] + bytes([payload[-1] ^ 0x01])

    def op_payload_edit_rehashed(self, trace, payload):
        """Change one letter inside a rendered body and fix the hash: only the body checks can tell."""
        if not payload:
            return
        text = payload.decode("utf-8")
        start = text.find(">\n")
        match = re.compile(r"[a-z]").search(text, start + 2) if start >= 0 else None
        if match:
            i = match.start()
            mutated = (text[:i] + text[i].upper() + text[i + 1:]).encode("utf-8")
            t = copy.deepcopy(trace)
            _rehash(t, mutated)
            yield f"payload/char-{i}", t, mutated

    def op_payload_drop_occurrence_consistent(self, trace, payload):
        """Remove the last rendered item from a fixture-xml payload and from included[], fixing the hash and the
        count, so the mutant is self-consistent: only conservation and protection checks can tell."""
        if not payload or self.snapshot.get("renderer") != "fixture-xml/v1" or not trace["included"]:
            return
        count = TOKENIZERS.get(self.snapshot.get("tokenizer"))
        text = payload.decode("utf-8")
        last = trace["included"][-1]
        try:
            parsed: Parsed = parse("fixture-xml/v1", payload)
        except ParseError:
            return
        occurrence = parsed.occurrences[-1]
        tag = occurrence.tag
        start = text.rfind(f"<{tag} id=")
        if start < 0 or count is None:
            return
        mutated = text[:start].encode("utf-8")
        t = copy.deepcopy(trace)
        t["included"].pop()
        t["result"]["input_tokens"] = count(text[:start])
        _rehash(t, mutated)
        if any(r["item_id"] == last["item_id"] for r in t["compressed"]):
            t["compressed"] = [r for r in t["compressed"] if r["item_id"] != last["item_id"]]
        yield "payload/-last-occurrence", t, mutated

    def op_payload_swap_occurrences_consistent(self, trace, payload):
        """Swap the first two adjacent rendered items in a fixture-xml payload and in included[], rehashed."""
        if not payload or self.snapshot.get("renderer") != "fixture-xml/v1":
            return
        text = payload.decode("utf-8")
        pieces = re.findall(r"<([A-Za-z_][A-Za-z0-9_.-]*) id=\"[^\"]*\"[^>]*>\n.*?\n</\1>\n", text, flags=re.S)
        blocks = re.findall(r"(<([A-Za-z_][A-Za-z0-9_.-]*) id=\"[^\"]*\"[^>]*>\n.*?\n</\2>\n)", text, flags=re.S)
        blocks = [b[0] for b in blocks]
        if len(blocks) != len(pieces) or "".join(blocks) != text or len(blocks) < 2:
            return
        for i in range(len(blocks) - 1):
            if blocks[i] != blocks[i + 1] and i + 1 < len(trace["included"]):
                swapped = blocks[:i] + [blocks[i + 1], blocks[i]] + blocks[i + 2:]
                mutated = "".join(swapped).encode("utf-8")
                t = copy.deepcopy(trace)
                t["included"][i], t["included"][i + 1] = t["included"][i + 1], t["included"][i]
                _rehash(t, mutated)
                yield f"payload/swap-{i}", t, mutated
                return
