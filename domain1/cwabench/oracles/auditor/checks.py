"""Checks A1–A16. Each returns the violations it finds; a check that cannot apply says why instead.

Every rule cited here is in SPEC.md or conformance/README.md. A check states only what must hold for every
conformant assembly, so it never needs to know which decisions an assembler should have made, only which ones it
could not have made.
"""
from __future__ import annotations

import hashlib
import json
import traceback
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from ...canon import digest as digest_mod
from ...canon import instants
from ...canon.payloads import ParseError, Parsed, parse, render_body, stream_of
from ...canon.strings import collapse, utf16_key
from ...canon.tokenizers import TOKENIZERS
from .model import (
    AUTHORITIES, EVIDENCE_SLOTS, POLICY_FIELDS, SLOT_AUTHORITY, SLOTS, TIERS, Candidate, View, as_double,
    canonical_key, dumps, stage_of,
)

CHECKS = {
    "A1": "Trace validates against trace.schema.json",
    "A2": "Conservation: every candidate and producer exclusion accounted for once",
    "A3": "Refusal shape",
    "A4": "Payload hash and snapshot digest",
    "A5": "Token accounting",
    "A6": "Protected integrity",
    "A7": "Tier order under budget pressure",
    "A8": "No synthesized text; compressed rows match variants",
    "A9": "Ordering of included, excluded, conflicts and defaults",
    "A10": "Reason codes are valid, shaped and justified",
    "A11": "Recovery action and evidence sufficiency",
    "A12": "Conflict records and their exclusions",
    "A13": "Supersession, deduplication and source-diversity rows",
    "A14": "Payload structure",
    "A15": "Defaults filled",
    "A16": "Context and provenance echo",
}
MIN_FIELDS = ("id", "slot", "source", "source_version", "authority", "freshness", "trust", "body")
PRE_FIT_REFUSALS = ("required_slot_missing", "protected_slot_unplaced", "conflict_unresolved",
                    "protected_content_over_budget")
ALLOWED_ROW_KEYS = {"item_id", "reason", "stage", "slot", "duplicate_of", "superseded_by"}
RESOLUTION_OF_ACTION = {"surface": "surfaced", "request_context": "context_requested", "refuse": "refused"}
# Admission conditions definite enough that any candidate meeting one must carry an exclusion row.
ADMISSION_CONDITIONS = (
    "producer_not_authenticated", "unknown_slot", "unknown_authority", "invalid_structure", "producer_slot_not_allowed",
    "authority_not_allowed", "untrusted_in_governance", "untrusted_content_unmarked", "protected_tier_changed",
    "tier_upgrade_not_allowed", "duplicate_variant_id", "revoked", "expired", "future_freshness", "stale_state",
    "not_eligible", "source_invalid", "out_of_scope", "below_threshold",
)


def _group_by_slot(candidates) -> list[tuple[str, list]]:
    """Consecutive runs of candidates sharing a slot, keeping their order; a slot appears once if its items are
    contiguous."""
    groups: dict[str, list] = {}
    for candidate in candidates:
        groups.setdefault(candidate.slot, []).append(candidate)
    return list(groups.items())


@dataclass
class CheckResult:
    status: str  # "pass", "fail" or "not_run"
    violations: list[str] = field(default_factory=list)
    reason: str | None = None  # why it did not run

    def as_check(self, id: str) -> dict:
        out = {"oracle": "auditor", "id": id, "status": self.status}
        if self.violations:
            out["detail"] = "; ".join(self.violations[:3]) + (f" (+{len(self.violations) - 3} more)"
                                                               if len(self.violations) > 3 else "")
        elif self.reason:
            out["detail"] = self.reason
        return out


@dataclass
class AuditResult:
    checks: dict[str, CheckResult]

    @property
    def status(self) -> str:
        return "fail" if any(c.status == "fail" for c in self.checks.values()) else "pass"

    @property
    def failed(self) -> list[str]:
        return [id for id, c in self.checks.items() if c.status == "fail"]

    def as_checks(self) -> list[dict]:
        return [result.as_check(id) for id, result in self.checks.items()]


class NotApplicable(Exception):
    pass


@dataclass(frozen=True)
class Decision:
    decided_by: str
    resolution: str
    winner: str | None
    excluded: list[str]


@dataclass
class Match:
    row_index: int
    row: dict
    placement_index: int
    placement: dict
    occurrence: object


class Audit:
    def __init__(self, contract, snapshot_bytes: bytes, payload: bytes | None, trace: dict):
        self.contract = contract
        self.snapshot = json.loads(snapshot_bytes.decode("utf-8"))
        self.view = View.build(contract, self.snapshot)
        self.payload = payload
        self.trace = trace
        self.count = TOKENIZERS.get(self.snapshot.get("tokenizer"))
        self.refused = bool((trace.get("refused") or {}).get("bool")) if isinstance(trace.get("refused"), dict) else False
        self.refusal = (trace.get("refused") or {}).get("reason") if self.refused else None
        self.included = [r for r in trace.get("included") or [] if isinstance(r, dict)]
        self.excluded = [r for r in trace.get("excluded") or [] if isinstance(r, dict)]
        self.compressed = [r for r in trace.get("compressed") or [] if isinstance(r, dict)]
        self.conflicts = [r for r in trace.get("conflicts") or [] if isinstance(r, dict)]
        self.assembler_rows = [r for r in self.excluded if r.get("stage") == "assembler"]
        self.parsed: Parsed | None = None
        self.parse_error: str | None = None
        self.matches: list[Match] | None = None
        self.match_error: str | None = None
        if payload is not None:
            try:
                self.parsed = parse(self.snapshot.get("renderer"), payload)
            except ParseError as error:
                self.parse_error = str(error)
            if self.parsed is not None:
                try:
                    self.matches = self._match()
                except ParseError as error:
                    self.match_error = str(error)

    # Shared derivations ------------------------------------------------------------------------------------------

    def _match(self) -> list[Match]:
        """Pair each included row with its rendered occurrence, placement by placement (conformance/README.md:
        included[] follows placement order and, within a placement, the renderer's item order)."""
        streams: dict[str, list] = defaultdict(list)
        for occurrence in self.parsed.occurrences:
            streams[occurrence.stream].append(occurrence)
        cursor = Counter()
        rows, r, out = self.included, 0, []
        for p_index, placement in enumerate(self.view.placements):
            stream, tag = stream_of(placement["wrap"])
            seen: set[str] = set()
            while r < len(rows) and rows[r].get("slot") == placement["slot"] and rows[r].get("item_id") not in seen:
                row = rows[r]
                seen.add(row.get("item_id"))
                if cursor[stream] >= len(streams[stream]):
                    raise ParseError(f"included row {r} ({row.get('item_id')}) has no occurrence in the {stream} stream")
                occurrence = streams[stream][cursor[stream]]
                cursor[stream] += 1
                if occurrence.id != row.get("item_id") or occurrence.tag != tag:
                    raise ParseError(f"included row {r} is {row.get('item_id')!r} in {placement['wrap']}, but the "
                                     f"payload has {occurrence.id!r} in {occurrence.tag or occurrence.stream}")
                out.append(Match(r, row, p_index, placement, occurrence))
                r += 1
        if r != len(rows):
            raise ParseError(f"included row {r} ({rows[r].get('slot')}) does not follow placement order")
        for stream, occurrences in streams.items():
            if cursor[stream] != len(occurrences):
                extra = occurrences[cursor[stream]]
                raise ParseError(f"the payload renders {extra.id!r} in the {stream} stream with no included row")
        return out

    def rows_by_id(self) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = defaultdict(list)
        for row in self.assembler_rows:
            out[row.get("item_id")].append(row)
        return out

    def excluded_stage(self) -> dict[str, int]:
        """The earliest pipeline stage that excluded each id, for ids with one candidate."""
        out: dict[str, int] = {}
        for row in self.assembler_rows:
            item_id = row.get("item_id")
            if self.view.unique(item_id) is not None:
                out[item_id] = min(out.get(item_id, 99), stage_of(row.get("reason")))
        return out

    def admitted(self) -> list[Candidate]:
        """Candidates admission let through (unique id, a valid slot, no admission row)."""
        stage = self.excluded_stage()
        return [c for c in self.view.candidates
                if self.view.unique(c.recorded_id) is c and c.slot is not None and stage.get(c.recorded_id, 99) > 0]

    def exempt(self, candidate: Candidate) -> bool:
        return self.view.tier(candidate) == "protected" or candidate.recorded_id in self.view.group_of

    def surfaced_group(self) -> dict[str, str]:
        """item id → group id, for members of groups the trace records as surfaced."""
        surfaced = {c.get("group_id") for c in self.conflicts if c.get("resolution") == "surfaced"}
        return {i: g["id"] for i, g in self.view.group_of.items() if g["id"] in surfaced}

    # Running ----------------------------------------------------------------------------------------------------

    def run(self) -> AuditResult:
        results = {}
        for id in CHECKS:
            method = getattr(self, "a" + id[1:])
            try:
                violations = method()
                results[id] = CheckResult("fail" if violations else "pass", violations)
            except NotApplicable as why:
                results[id] = CheckResult("not_run", reason=str(why))
            except Exception as error:  # a bug in the auditor must surface, never pass silently
                tail = traceback.format_exception_only(type(error), error)[-1].strip()
                results[id] = CheckResult("fail", [f"auditor error: {tail}"])
        return AuditResult(results)

    # A1 ---------------------------------------------------------------------------------------------------------

    def a1(self) -> list[str]:
        validator = self.contract.validator("trace.schema.json")
        errors = sorted(validator.iter_errors(self.trace), key=lambda e: list(e.absolute_path))
        return [f"/{'/'.join(map(str, e.absolute_path))}: {e.message[:200]}" for e in errors[:5]]

    # A2 ---------------------------------------------------------------------------------------------------------

    def a2(self) -> list[str]:
        out = []
        for row in self.excluded:
            if row.get("stage") not in ("producer", "assembler"):
                out.append(f"row {row.get('item_id')!r} has stage {row.get('stage')!r}")
        reported = Counter(canonical_key(row) for _, row in self.view.producer_rows)
        recorded = Counter(canonical_key(row) for row in self.excluded if row.get("stage") == "producer")
        if reported != recorded:
            missing = sum((reported - recorded).values())
            extra = sum((recorded - reported).values())
            out.append(f"producer rows differ from the batches' reports ({missing} missing, {extra} not reported)")

        rows = Counter(row.get("item_id") for row in self.assembler_rows)
        included_ids = {row.get("item_id") for row in self.included}
        for item_id, n in rows.items():
            have = len(self.view.by_id.get(item_id, []))
            if have == 0:
                out.append(f"excluded row for {item_id!r}, which no candidate is")
            elif n > have:
                out.append(f"{item_id!r} has {n} excluded rows for {have} candidate(s)")
        for item_id in included_ids:
            have = len(self.view.by_id.get(item_id, []))
            if have != 1:
                out.append(f"included {item_id!r}, which {have} candidates are")
            elif rows.get(item_id):
                out.append(f"{item_id!r} is both included and excluded")
            elif item_id in self.view.producer_exclusion_ids:
                out.append(f"included {item_id!r}, which a producer exclusion also uses (duplicate_item_id)")
        if not self.refused:
            for item_id, candidates in self.view.by_id.items():
                accounted = rows.get(item_id, 0) + (item_id in included_ids)
                if accounted != len(candidates):
                    out.append(f"{item_id!r}: {len(candidates)} candidate(s), {accounted} accounted for")
        return out

    # A3 ---------------------------------------------------------------------------------------------------------

    def a3(self) -> list[str]:
        refused = self.trace.get("refused")
        if not isinstance(refused, dict) or not isinstance(refused.get("bool"), bool):
            return ["refused.bool is not a boolean"]
        out = []
        if refused["bool"]:
            if self.payload is not None:
                out.append("refused, but a payload was returned")
            if self.trace.get("result") is not None:
                out.append("refused, but result is not null")
            if self.trace.get("included") != [] or self.trace.get("compressed") != []:
                out.append("refused, but included or compressed is not empty")
            if not isinstance(refused.get("reason"), str) or not refused["reason"]:
                out.append("refused without a reason")
        else:
            if refused.get("reason") is not None:
                out.append("assembled, but refused.reason is not null")
            if self.payload is None:
                out.append("assembled, but no payload was returned")
            if not isinstance(self.trace.get("result"), dict):
                out.append("assembled, but result is not an object")
        return out

    # A4 ---------------------------------------------------------------------------------------------------------

    def a4(self) -> list[str]:
        out = []
        expected = digest_mod.snapshot_digest(self.snapshot)
        recorded = (self.trace.get("context") or {}).get("snapshot_digest")
        if recorded != expected:
            out.append(f"snapshot_digest {str(recorded)[:16]}… is not the snapshot's {expected[:16]}…")
        result = self.trace.get("result")
        if self.payload is not None and isinstance(result, dict):
            actual = hashlib.sha256(self.payload).hexdigest()
            if result.get("hash") != actual:
                out.append(f"result.hash {str(result.get('hash'))[:16]}… is not the payload's {actual[:16]}…")
        return out

    # A5 ---------------------------------------------------------------------------------------------------------

    def _need_payload(self) -> None:
        if self.refused or self.payload is None:
            raise NotApplicable("no payload")
        if self.parsed is None:
            raise NotApplicable(f"payload did not parse: {self.parse_error}")
        if self.matches is None:
            raise NotApplicable(f"payload does not match included[]: {self.match_error}")

    def a5(self) -> list[str]:
        self._need_payload()
        if self.count is None:
            raise NotApplicable(f"tokenizer {self.snapshot.get('tokenizer')} is not one the auditor counts")
        out = []
        total = sum(self.count(text) for text in self.parsed.counted)
        result = self.trace.get("result") or {}
        if result.get("input_tokens") != total:
            out.append(f"result.input_tokens is {result.get('input_tokens')}, the payload counts {total}")
        for match in self.matches:
            tokens = self.count(match.occurrence.rendered)
            if match.row.get("tokens") != tokens:
                out.append(f"included {match.row.get('item_id')!r} records {match.row.get('tokens')} tokens, "
                           f"its rendered body counts {tokens}")
        margin = (self.snapshot["budget"]).get("margin_percent", 0)
        charged = (total * (100 + margin) + 99) // 100
        if charged > self.snapshot["budget"]["input"]:
            out.append(f"charged count {charged} exceeds budget.input {self.snapshot['budget']['input']}")
        return out

    # A6 ---------------------------------------------------------------------------------------------------------

    def a6(self) -> list[str]:
        out = []
        late = ("over_budget", "conflict_lost", "conflict_deferred", "superseded", "duplicate_content",
                "source_diversity_cap")
        for row in self.assembler_rows:
            candidate = self.view.unique(row.get("item_id"))
            if candidate and row.get("reason") in late and self.view.tier(candidate) == "protected":
                out.append(f"protected {candidate.recorded_id!r} excluded with {row.get('reason')}")
        if self.refused:
            if self.refusal in PRE_FIT_REFUSALS and any(r.get("reason") == "over_budget" for r in self.assembler_rows):
                out.append(f"{self.refusal} comes before fitting, yet the trace has over_budget rows")
            return out
        included = {row.get("item_id") for row in self.included}
        compressed = {row.get("item_id") for row in self.compressed}
        for candidate in self.admitted():
            if self.view.tier(candidate) != "protected" or candidate.recorded_id in self.excluded_stage():
                continue
            if candidate.recorded_id not in included:
                out.append(f"admitted protected {candidate.recorded_id!r} is not in the payload")
            if candidate.recorded_id in compressed:
                out.append(f"protected {candidate.recorded_id!r} was compressed")
        for match in self.matches or []:
            candidate = self.view.unique(match.row.get("item_id"))
            if candidate and self.view.tier(candidate) == "protected":
                body = candidate.get("body")
                if isinstance(body, str) and match.occurrence.body != body:
                    out.append(f"protected {candidate.recorded_id!r} renders a body other than its own")
        return out

    # A7 ---------------------------------------------------------------------------------------------------------

    def a7(self) -> list[str]:
        if self.refused:
            raise NotApplicable("refused: nothing was kept to compare")
        if self.count is None:
            raise NotApplicable("tokenizer not counted by the auditor")
        pressured = []
        reduced = [r.get("item_id") for r in self.assembler_rows if r.get("reason") == "over_budget"]
        reduced += [r.get("item_id") for r in self.compressed]
        for item_id in reduced:
            candidate = self.view.unique(item_id)
            if candidate is None or self.view.tier(candidate) != "compressible":
                continue
            if self.view.cap_bound(candidate, self.count):
                continue  # may be its token_budget (step 2) or its slot cap (step 3), which run regardless of fit
            pressured.append(item_id)
        if not pressured:
            return []
        out = []
        for row in self.included:
            candidate = self.view.unique(row.get("item_id"))
            if candidate and self.view.tier(candidate) == "droppable" and "min_tokens" not in self.view.rules(candidate.slot):
                out.append(f"droppable {candidate.recorded_id!r} kept while compressible {pressured[0]!r} was reduced "
                           "for budget pressure")
        return out

    # A8 ---------------------------------------------------------------------------------------------------------

    def a8(self) -> list[str]:
        self._need_payload()
        out, k = [], 0
        for match in self.matches:
            candidate = self.view.unique(match.row.get("item_id"))
            body = candidate.get("body") if candidate else None
            if not isinstance(body, str):
                continue
            stream = match.occurrence.stream
            if match.occurrence.rendered == render_body(body, stream):
                continue
            variants = [v for v in self.view.filled(candidate, "variants") or []
                        if isinstance(v, dict) and isinstance(v.get("body"), str)
                        and render_body(v["body"], stream) == match.occurrence.rendered]
            if not variants:
                out.append(f"{candidate.recorded_id!r} renders text that is neither its body nor a supplied variant")
                continue
            if k >= len(self.compressed):
                out.append(f"{candidate.recorded_id!r} renders a variant but has no compressed row")
                continue
            row = self.compressed[k]
            k += 1
            chosen = next((v for v in variants if v.get("id") == row.get("variant_id")), None)
            if row.get("item_id") != candidate.recorded_id or row.get("slot") != match.row.get("slot"):
                out.append(f"compressed row {k - 1} is {row.get('item_id')!r}, expected {candidate.recorded_id!r}")
            elif chosen is None:
                out.append(f"compressed {candidate.recorded_id!r} names variant {row.get('variant_id')!r}, which is "
                           "not the one rendered")
            else:
                if row.get("method") != chosen.get("method"):
                    out.append(f"compressed {candidate.recorded_id!r} method {row.get('method')!r} is not the variant's")
                if self.count is not None:
                    original = self.count(render_body(body, stream))
                    rendered = self.count(match.occurrence.rendered)
                    if row.get("from") != original or row.get("to") != rendered:
                        out.append(f"compressed {candidate.recorded_id!r} from/to {row.get('from')}/{row.get('to')}, "
                                   f"rendered {original}/{rendered}")
        if k != len(self.compressed):
            out.append(f"{len(self.compressed) - k} compressed row(s) match no compressed occurrence")
        return out

    # A9 ---------------------------------------------------------------------------------------------------------

    def a9(self) -> list[str]:
        out = []
        if self.matches is not None:
            runs: dict[int, list[Match]] = defaultdict(list)
            for match in self.matches:
                runs[match.placement_index].append(match)
            for matches in runs.values():
                slot = matches[0].placement["slot"]
                keys = []
                for match in matches:
                    candidate = self.view.unique(match.row.get("item_id"))
                    item_id = match.row.get("item_id")
                    if slot == "interaction.history":
                        keys.append((self.view.freshness(candidate) if candidate else None, utf16_key(item_id)))
                    else:
                        keys.append(utf16_key(item_id))
                if any(a is None for key in keys for a in (key if isinstance(key, tuple) else (key,))):
                    continue
                if keys != sorted(keys) or len(set(keys)) != len(keys):
                    out.append(f"{slot} items are not in the renderer's order")
        elif self.match_error and not self.refused:
            out.append(f"included[] does not follow the payload: {self.match_error}")

        # Producer rows first, in producer, item id, serialization order.
        stages = [0 if row.get("stage") == "producer" else 1 for row in self.excluded]
        if stages != sorted(stages):
            out.append("producer rows do not all come before assembler rows")
        expected = [canonical_key(row) for _, row in sorted(
            self.view.producer_rows, key=lambda pr: (utf16_key(pr[0]), utf16_key(str(pr[1].get("item_id"))), canonical_key(pr[1])))]
        actual = [canonical_key(row) for row in self.excluded if row.get("stage") == "producer"]
        if sorted(expected) == sorted(actual) and expected != actual:
            out.append("producer rows are not ordered by producer, item id, serialization")

        # Assembler rows by pipeline stage, then the stage's own order.
        order = [stage_of(row.get("reason")) for row in self.assembler_rows]
        if order != sorted(order):
            out.append("assembler rows are not in pipeline order (admission, conflict, supersede, dedupe, diversity, fit)")
        admission = [row for row in self.assembler_rows if stage_of(row.get("reason")) == 0]
        keys, used = [], Counter()
        for row in admission:
            item_id = row.get("item_id")
            candidates = sorted(self.view.by_id.get(item_id, []), key=lambda c: (utf16_key(c.producer), c.canonical))
            if used[item_id] < len(candidates):
                candidate = candidates[used[item_id]]
                used[item_id] += 1
                keys.append((utf16_key(candidate.producer), utf16_key(item_id), candidate.canonical))
        if keys != sorted(keys):
            out.append("admission rows are not ordered by producer, item id, candidate serialization")
        for stage in (1, 2, 3, 4):
            ids = [utf16_key(str(row.get("item_id"))) for row in self.assembler_rows if stage_of(row.get("reason")) == stage]
            if ids != sorted(ids):
                out.append(f"{('', 'conflict', 'supersession', 'deduplication', 'source-diversity')[stage]} rows are "
                           "not ordered by item id")

        # Fitting rows follow the order items were omitted. Under budget pressure, droppable items go first, in
        # shedding order (slots by priority then name, each from its lowest rank up), and within a slot compressible
        # items are omitted from the lowest rank up. Reductions a cap may explain are left out: they come earlier.
        pressure = []
        for row in self.assembler_rows:
            candidate = self.view.unique(row.get("item_id"))
            if row.get("reason") == "over_budget" and candidate and candidate.slot \
                    and not self.view.cap_bound(candidate, self.count):
                pressure.append(candidate)
        dropped = [c for c in pressure if self.view.tier(c) == "droppable"]
        squeezed = [c for c in pressure if self.view.tier(c) == "compressible"]
        expected_drops = sorted(dropped, key=lambda c: self.view.slot_order_key(c.slot))
        expected_drops = [c for _, group in _group_by_slot(expected_drops) for c in
                          sorted(group, key=self.view.rank_key, reverse=True)]
        if [c.recorded_id for c in dropped] != [c.recorded_id for c in expected_drops]:
            out.append("droppable items were not omitted in shedding order")
        if dropped and squeezed and pressure.index(squeezed[0]) < pressure.index(dropped[-1]):
            out.append("a compressible item was omitted for budget before every droppable one")
        for slot, group in _group_by_slot(squeezed):
            if [c.recorded_id for c in group] != [c.recorded_id for c in sorted(group, key=self.view.rank_key, reverse=True)]:
                out.append(f"compressible items in {slot} were not omitted from the lowest rank up")

        groups = [utf16_key(str(c.get("group_id"))) for c in self.conflicts]
        if groups != sorted(groups) or len(set(groups)) != len(groups):
            out.append("conflicts are not ordered by group_id")
        fields = [(utf16_key(str(r.get("item_id"))), POLICY_FIELDS.index(r.get("field")) if r.get("field") in POLICY_FIELDS else 99)
                  for r in self.trace.get("defaults_filled") or [] if isinstance(r, dict)]
        if fields != sorted(fields):
            out.append("defaults_filled is not ordered by item id, then field in R-3 order")
        return out

    # A10 --------------------------------------------------------------------------------------------------------

    def _scope_ok(self, candidate: Candidate) -> bool:
        request = self.snapshot.get("scope") or {}
        scope = candidate.get("scope") or {}
        if not isinstance(scope, dict):
            return False
        if any(key not in request or request[key] != value for key, value in scope.items()):
            return False
        return all(key in scope for key in self.view.rules(candidate.slot).get("required_scope", []))

    def _age_exceeds(self, candidate: Candidate) -> bool:
        limit = self.view.rules(candidate.slot).get("max_age_seconds")
        fresh = self.view.freshness(candidate)
        return limit is not None and fresh is not None and self.view.assembly_time - fresh > limit

    def _verified_mcp(self, candidate: Candidate) -> bool:
        listed = (self.view.route.get("producers") or {}).get(candidate.producer) or {}
        return candidate.producer_kind == "mcp" and listed.get("verified") is True

    def _kind_allows(self, candidate: Candidate, slot: str) -> bool:
        kind = candidate.producer_kind
        if slot.startswith("state.") and kind != "state":
            return False
        if kind == "memory" and slot != "interaction.memory":
            return False
        if kind == "retrieval" and slot not in EVIDENCE_SLOTS:
            return False
        if kind == "mcp" and slot not in EVIDENCE_SLOTS + ("governance.capabilities",):
            return False
        return True

    def _justified(self, candidate: Candidate, reason: str) -> bool | None:
        """Whether the condition a reason names holds for the item. None when the auditor cannot tell."""
        view, item = self.view, candidate.item
        slot = candidate.slot
        if reason == "producer_not_authenticated":
            return not view.admitted_producer(candidate)
        if reason.startswith("missing_field:"):
            name = reason.split(":", 1)[1]
            required = name in MIN_FIELDS or (name == "expires" and slot == "interaction.memory") or (
                name == "relevance" and slot == "evidence.knowledge")
            return candidate.is_object and name not in item and required
        if reason == "unknown_slot":
            return not (isinstance(candidate.get("slot"), str) and candidate.get("slot") in SLOTS)
        if reason == "unknown_authority":
            return candidate.get("authority") not in AUTHORITIES
        if reason == "invalid_structure":
            return not view.schema_valid(candidate)
        if reason == "duplicate_item_id":
            return len(view.by_id[candidate.recorded_id]) + (candidate.recorded_id in view.producer_exclusion_ids) >= 2
        if slot is None:
            return None
        if reason == "producer_slot_not_allowed":
            listed = (view.route.get("producers") or {}).get(candidate.producer) or {}
            return slot not in listed.get("slots", []) or not self._kind_allows(candidate, slot)
        if reason == "authority_not_allowed":
            authority = candidate.get("authority")
            generated_turn = slot == "interaction.history" and view.filled(candidate, "lineage") == "generated"
            return authority not in SLOT_AUTHORITY[slot] or (generated_turn and authority != "untrusted")
        if reason == "capability_not_allowed":
            return slot == "governance.capabilities"
        if reason == "untrusted_in_governance":
            return slot.startswith("governance.") and (
                candidate.get("trust") != "verified" or view.filled(candidate, "injection_risk") == "untrusted_content")
        if reason == "untrusted_content_unmarked":
            return (self.contract.slot_defaults[slot]["injection_risk"] == "untrusted_content"
                    and view.filled(candidate, "injection_risk") != "untrusted_content" and not self._verified_mcp(candidate))
        if reason == "protected_tier_changed":
            own = candidate.get("tier")
            return self.contract.slot_defaults[slot]["tier"] == "protected" and own in TIERS and own != "protected"
        if reason == "tier_upgrade_not_allowed":
            own = candidate.get("tier")
            return own in TIERS and TIERS.index(own) > TIERS.index(view.slot_tier(slot))
        if reason == "duplicate_variant_id":
            ids = [v.get("id") for v in candidate.get("variants") or [] if isinstance(v, dict)]
            return len(ids) != len(set(ids)) or candidate.recorded_id in ids
        if reason == "revoked":
            return "revoked_by" in item
        if reason == "expired":
            expires = instants.try_parse(candidate.get("expires"))
            return expires is not None and expires <= view.assembly_time
        if reason == "future_freshness":
            fresh = view.freshness(candidate)
            return fresh is not None and fresh > view.assembly_time + view.clock_skew
        if reason == "stale_state":
            return slot.startswith("state.") and self._age_exceeds(candidate)
        if reason == "not_eligible":
            return not slot.startswith("state.") and self._age_exceeds(candidate)
        if reason == "source_invalid":
            prefix = view.rules(slot).get("source_prefix")
            return prefix is not None and not str(candidate.get("source")).startswith(prefix)
        if reason == "out_of_scope":
            return not self._scope_ok(candidate)
        if reason == "below_threshold":
            threshold = as_double(view.rules(slot).get("min_relevance"))
            score = as_double(candidate.get("relevance"))
            return threshold is not None and (score is None or score < threshold)
        if reason == "slot_unplaced":
            return slot not in view.placed_slots
        return None

    def _sound(self, candidate: Candidate) -> list[str]:
        """Exclusion conditions that hold for an included item, which no conformant assembler would include."""
        broken = []
        for reason in ("producer_not_authenticated", "producer_slot_not_allowed", "authority_not_allowed",
                       "untrusted_in_governance", "untrusted_content_unmarked", "revoked", "expired",
                       "future_freshness", "stale_state", "not_eligible", "source_invalid", "out_of_scope",
                       "below_threshold", "slot_unplaced", "duplicate_variant_id"):
            if self._justified(candidate, reason):
                broken.append(reason)
        return broken

    def a10(self) -> list[str]:
        out = []
        codes = {r["code"]: r["kind"] for r in self.contract.reasons}
        for row in self.excluded:
            extra = set(row) - ALLOWED_ROW_KEYS
            if extra:
                out.append(f"row {row.get('item_id')!r} has fields {sorted(extra)}")
            reason = row.get("reason")
            template = self.contract.reason_template(reason) if isinstance(reason, str) else None
            if template is None or codes.get(template) != "exclusion":
                out.append(f"row {row.get('item_id')!r} reason {reason!r} is not an exclusion code")
                continue
            if row.get("stage") == "assembler":
                if ("duplicate_of" in row) != (reason == "duplicate_content"):
                    out.append(f"row {row.get('item_id')!r}: duplicate_of goes with duplicate_content only")
                if ("superseded_by" in row) != (reason == "superseded"):
                    out.append(f"row {row.get('item_id')!r}: superseded_by goes with superseded only")
        if self.refused and codes.get(self.refusal) != "refusal":
            out.append(f"refusal reason {self.refusal!r} is not a refusal code")

        by_id = self.rows_by_id()
        for item_id, rows in by_id.items():
            candidates = self.view.by_id.get(item_id, [])
            if not candidates:
                continue
            row_slots = Counter(row.get("slot") for row in rows)
            if len(candidates) == len(rows):
                cand_slots = Counter(c.slot for c in candidates)
                if row_slots != cand_slots:
                    out.append(f"{item_id!r}: rows carry slots {dict(row_slots)}, the candidates name {dict(cand_slots)}")
            if len(candidates) != 1:
                continue
            candidate = candidates[0]
            for row in rows:
                reason = row.get("reason")
                if isinstance(reason, str) and stage_of(reason) == 0:
                    verdict = self._justified(candidate, reason)
                    if verdict is False:
                        out.append(f"{item_id!r} excluded with {reason}, whose condition does not hold")
        for row in self.included:
            candidate = self.view.unique(row.get("item_id"))
            if candidate:
                broken = self._sound(candidate)
                if broken:
                    out.append(f"{candidate.recorded_id!r} is included although {', '.join(broken)} applies")
        if self.refused:
            # A refused trace keeps every admission row, but admitted items have none, so an item without a row
            # must have passed admission.
            for item_id, candidates in self.view.by_id.items():
                if by_id.get(item_id):
                    continue
                if len(candidates) + (item_id in self.view.producer_exclusion_ids) >= 2:
                    out.append(f"{item_id!r} is shared by several candidates but has no duplicate_item_id row")
                    continue
                failing = [r for r in ADMISSION_CONDITIONS if self._justified(candidates[0], r)]
                if self._missing_field(candidates[0]):
                    failing.append("missing_field")
                if candidates[0].slot and candidates[0].slot not in self.view.placed_slots \
                        and self.view.tier(candidates[0]) != "protected":
                    failing.append("slot_unplaced")
                if failing:
                    out.append(f"{item_id!r} has no exclusion row although {', '.join(failing)} applies")
        return out

    def _missing_field(self, candidate: Candidate) -> bool:
        if not candidate.is_object:
            return False
        required = list(MIN_FIELDS)
        if candidate.slot == "interaction.memory":
            required.append("expires")
        if candidate.slot == "evidence.knowledge":
            required.append("relevance")
        return any(name not in candidate.item for name in required)

    # A11 --------------------------------------------------------------------------------------------------------

    def a11(self) -> list[str]:
        out = []
        recovery = self.trace.get("recovery")
        action = recovery.get("action") if isinstance(recovery, dict) else None
        if self.refusal == "evidence_required":
            omitted = [r.get("item_id") for r in self.assembler_rows
                       if r.get("reason") == "over_budget" and r.get("slot") in EVIDENCE_SLOTS]
            if not omitted:
                expected = "request_context"
            elif any(not (self.view.filled(c, "variants") or []) for c in map(self.view.unique, omitted) if c):
                expected = "precompute_summary"
            else:
                expected = "retrieve_narrower"
            if action != expected:
                out.append(f"evidence_required records recovery {action!r}; the omissions call for {expected!r}")
        elif self.refusal == "conflict_unresolved":
            blocking = [c for c in self.conflicts if c.get("resolution") in ("context_requested", "refused")]
            asked = bool(blocking) and all(c.get("resolution") == "context_requested" for c in blocking)
            if asked and action != "request_context":
                out.append("every blocking group asked for context, but recovery.action is not request_context")
            if not asked and recovery is not None:
                out.append("recovery is recorded although not every blocking group asked for context")
        elif recovery is not None:
            out.append(f"recovery recorded on a {'refusal ' + str(self.refusal) if self.refused else 'successful assembly'}")

        # The refusal reason is the earliest condition that holds, in reasons.json order (R-21). The first three
        # conditions can be computed exactly from admission and the conflict decisions.
        admitted = self.admitted()
        required = ["governance.instructions", "interaction.query"]
        if self.view.route.get("parser"):
            required.append("governance.output_contract")
        held = {
            "required_slot_missing": any(slot not in {c.slot for c in admitted} for slot in required),
            "protected_slot_unplaced": any(c.slot not in self.view.placed_slots and self.view.tier(c) == "protected"
                                           for c in admitted),
            "conflict_unresolved": any(d.resolution in ("context_requested", "refused")
                                       for d in self.expected_conflicts().values()),
        }
        first = next((code for code, holds in held.items() if holds), None)
        if first is not None and self.refusal != first:
            out.append(f"{first} holds, so assembly must refuse with it, not {self.refusal!r}"
                       if self.refused else f"{first} holds, yet assembly did not refuse")
        elif first is None and self.refused:
            if self.refusal in held:
                out.append(f"refused with {self.refusal}, whose condition does not hold")
            elif self.refusal == "slot_floor_over_budget" and not any(
                    "min_tokens" in (rules or {}) for rules in (self.view.route.get("slots") or {}).values()):
                out.append("slot_floor_over_budget on a route that sets no min_tokens")
            elif self.refusal == "evidence_required" and not self.view.route.get("requires_evidence"):
                out.append("evidence_required on a route that does not require evidence")

        if not self.refused and self.view.route.get("requires_evidence"):
            ids = {r.get("item_id") for r in self.included if r.get("slot") in EVIDENCE_SLOTS}
            if not ids:
                out.append("the route requires evidence and none was included")
            for slot in EVIDENCE_SLOTS:
                need = self.view.rules(slot).get("min_included")
                have = len({r.get("item_id") for r in self.included if r.get("slot") == slot})
                if need is not None and have < need:
                    out.append(f"{slot} includes {have} item(s), below min_included {need}")
        return out

    # A12 --------------------------------------------------------------------------------------------------------

    def expected_conflicts(self) -> dict[str, "Decision"]:
        """Each declared group's decision, computed from the rules in conformance/README.md, Conflicts."""
        if hasattr(self, "_decisions"):
            return self._decisions
        admitted = {c.recorded_id: c for c in self.admitted()}
        out = {}
        for group in self.snapshot.get("conflicts", []):
            members = [admitted[i] for i in sorted(set(group["items"]), key=utf16_key) if i in admitted]
            out[group["id"]] = self._decide(group, members)
        self._decisions = out
        return out

    def _decide(self, group: dict, members: list[Candidate]) -> "Decision":
        if len(members) < 2:
            return Decision("moot", "moot", None, [])
        winner, losers, decided = None, [], None
        if group.get("kind") == "instruction":
            instructing = [m for m in members if m.get("authority") in ("governing", "user")]
            top = "governing" if any(m.get("authority") == "governing" for m in instructing) else "user"
            peers = [m for m in instructing if m.get("authority") == top]
            if len(peers) <= 1:
                return Decision("authority", "resolved", peers[0].recorded_id if peers else None, [])
            policies = [self.view.filled(p, "conflict_policy") for p in peers]
            if policies.count("governs") == 1 and policies.count("defers") == len(peers) - 1:
                winner = peers[policies.index("governs")]
                losers, decided = [p for p in peers if p is not winner], "policy"
            action = self.view.route.get("on_unresolved_instruction", "refuse")
        else:
            policy = (self.view.route.get("facts") or {}).get(group.get("fact")) or {}
            precedence = policy.get("precedence", [])
            scope_keys = policy.get("scope", [])
            eligible = [m for m in members if m.producer in precedence
                        and all(k in (m.get("scope") or {}) for k in scope_keys)]
            if eligible:
                best = min(precedence.index(m.producer) for m in eligible)
                leaders = [m for m in eligible if precedence.index(m.producer) == best]
                if len(leaders) == 1:
                    winner, decided = leaders[0], "policy"
                elif policy.get("freshness_tiebreak"):
                    times = [self.view.freshness(m) for m in leaders]
                    latest = max(times)
                    if times.count(latest) == 1:
                        winner, decided = leaders[times.index(latest)], "freshness"
            if winner is not None:
                losers = [m for m in members if m is not winner]
            action = policy.get("on_unresolved")
        if winner is not None and not any(self.view.tier(m) == "protected" for m in losers):
            return Decision(decided, "resolved", winner.recorded_id, sorted((m.recorded_id for m in losers), key=utf16_key))
        return Decision("escalated", RESOLUTION_OF_ACTION.get(action, f"<{action}>"), None, [])

    def a12(self) -> list[str]:
        out = []
        groups = {g["id"]: g for g in self.snapshot.get("conflicts", [])}
        records = {c.get("group_id"): c for c in self.conflicts}
        if set(records) != set(groups) or len(records) != len(self.conflicts):
            out.append("conflicts[] does not hold exactly one record per declared group")
        lost: dict[str, list] = defaultdict(list)
        for row in self.assembler_rows:
            if row.get("reason") in ("conflict_lost", "conflict_deferred"):
                group = self.view.group_of.get(row.get("item_id"))
                if group is None:
                    out.append(f"{row.get('item_id')!r} excluded with {row.get('reason')} but is in no conflict group")
                else:
                    lost[group["id"]].append(row)
        for group_id, decision in self.expected_conflicts().items():
            group, record = groups[group_id], records.get(group_id)
            if record is None:
                continue
            name = f"group {group_id!r}"
            if record.get("kind") != group.get("kind"):
                out.append(f"{name} kind {record.get('kind')!r} is not the declared {group.get('kind')!r}")
            if record.get("items") != sorted(group["items"], key=utf16_key):
                out.append(f"{name} items are not every id it names, in id order")
            actual = (record.get("decided_by"), record.get("resolution"), record.get("winner"))
            expected = (decision.decided_by, decision.resolution, decision.winner)
            if actual != expected:
                out.append(f"{name} recorded {actual}, the rules give {expected}")
            reason = "conflict_lost" if group.get("kind") == "fact" else "conflict_deferred"
            excluded = sorted((r.get("item_id") for r in lost.get(group_id, [])), key=utf16_key)
            if excluded != decision.excluded:
                out.append(f"{name} excluded {excluded}, the rules exclude {decision.excluded}")
            if any(r.get("reason") != reason for r in lost.get(group_id, [])):
                out.append(f"{name}: members of a {group.get('kind')} group lose with {reason}")
        return out

    # A13 --------------------------------------------------------------------------------------------------------

    def a13(self) -> list[str]:
        out = []
        stage = self.excluded_stage()
        admitted = self.admitted()

        def alive(candidate: Candidate, through: int) -> bool:
            return stage.get(candidate.recorded_id, 99) > through

        for row in self.assembler_rows:
            reason, candidate = row.get("reason"), self.view.unique(row.get("item_id"))
            if candidate is None or reason not in ("superseded", "duplicate_content", "source_diversity_cap"):
                continue
            rules = self.view.rules(candidate.slot)
            name = candidate.recorded_id
            if self.exempt(candidate):
                out.append(f"exempt {name!r} excluded with {reason}")
            if reason == "superseded":
                target = self.view.unique(row.get("superseded_by"))
                if rules.get("supersede") != "source":
                    out.append(f"{name!r} superseded in {candidate.slot}, which does not supersede")
                elif target is None or target.slot != candidate.slot or target.producer != candidate.producer \
                        or target.get("source") != candidate.get("source") or not alive(target, 2):
                    out.append(f"{name!r} superseded_by {row.get('superseded_by')!r}, not a kept call of the same "
                               "producer and source")
                elif not (self.view.freshness(target) and self.view.freshness(candidate)
                          and self.view.freshness(target) > self.view.freshness(candidate)):
                    out.append(f"{name!r} superseded_by {target.recorded_id!r}, which is not later")
            elif reason == "duplicate_content":
                target = self.view.unique(row.get("duplicate_of"))
                if rules.get("dedupe") != "exact":
                    out.append(f"{name!r} deduplicated in {candidate.slot}, which does not dedupe")
                elif target is None or target.slot != candidate.slot or not alive(target, 3) \
                        or collapse(str(target.get("body"))) != collapse(str(candidate.get("body"))):
                    out.append(f"{name!r} duplicate_of {row.get('duplicate_of')!r}, not a kept item with the same body")
            elif reason == "source_diversity_cap" and rules.get("max_per_source") is None:
                out.append(f"{name!r} capped in {candidate.slot}, which has no max_per_source")

        by_slot: dict[str, list[Candidate]] = defaultdict(list)
        for candidate in admitted:
            by_slot[candidate.slot].append(candidate)
        for slot, candidates in by_slot.items():
            rules = self.view.rules(slot)
            if rules.get("supersede") == "source":
                calls = defaultdict(list)
                for c in candidates:
                    if alive(c, 1):
                        calls[(c.producer, c.get("source"))].append(c)
                for call in calls.values():
                    times = [self.view.freshness(c) for c in call]
                    if None in times:
                        continue
                    latest = max(times)
                    for c, t in zip(call, times):
                        if t < latest and not self.exempt(c) and alive(c, 2):
                            out.append(f"{c.recorded_id!r} kept though a later call from its source is in {slot}")
            if rules.get("dedupe") == "exact":
                keys = defaultdict(list)
                for c in candidates:
                    if alive(c, 3) and isinstance(c.get("body"), str):
                        keys[collapse(c.get("body"))].append(c)
                for same in keys.values():
                    if len(same) > 1 and not all(self.exempt(c) for c in same):
                        out.append(f"{[c.recorded_id for c in same]} share a body in {slot} and were all kept")
            cap = rules.get("max_per_source")
            if cap is not None:
                sources = defaultdict(list)
                for c in candidates:
                    if alive(c, 3):
                        sources[(c.producer, c.get("source"))].append(c)
                for items in sources.values():
                    exempt = sum(self.exempt(c) for c in items)
                    kept = [c for c in items if not self.exempt(c) and alive(c, 4)]
                    capped = [c for c in items if stage.get(c.recorded_id) == 4]
                    room = max(0, cap - exempt)
                    if len(kept) > room:
                        out.append(f"{len(kept)} items from one source kept in {slot}, cap leaves {room}")
                    if capped and len(kept) != room:
                        out.append(f"items capped in {slot} while the source had room")
        return out

    # A14 --------------------------------------------------------------------------------------------------------

    def a14(self) -> list[str]:
        if self.refused or self.payload is None:
            raise NotApplicable("no payload")
        if self.parse_error:
            return [f"payload is not what {self.snapshot.get('renderer')} writes: {self.parse_error}"]
        if self.match_error:
            return [f"payload does not match included[]: {self.match_error}"]
        out = []
        renderer = self.snapshot.get("renderer")
        surfaced = self.surfaced_group()
        for match in self.matches:
            occurrence, slot = match.occurrence, match.placement["slot"]
            candidate = self.view.unique(occurrence.id)
            if occurrence.stream == "system" and not slot.startswith("governance."):
                out.append(f"{slot} rendered in the system role")
            if occurrence.stream == "tools" and slot != "governance.capabilities":
                out.append(f"{slot} rendered in the tools role")
            if occurrence.conflict != surfaced.get(occurrence.id):
                out.append(f"{occurrence.id!r} conflict mark {occurrence.conflict!r}, expected {surfaced.get(occurrence.id)!r}")
            if occurrence.stream == "xml" and renderer != "fixture-xml/v1" and slot == "interaction.history":
                lineage = self.view.filled(candidate, "lineage") if candidate else None
                expected = "assistant" if lineage == "generated" else "user"
                if occurrence.speaker != expected:
                    out.append(f"history {occurrence.id!r} speaker {occurrence.speaker!r}, expected {expected!r}")
            elif occurrence.speaker is not None:
                out.append(f"{occurrence.id!r} has a speaker outside history")
        return out

    # A15 --------------------------------------------------------------------------------------------------------

    def a15(self) -> list[str]:
        expected = []
        for candidate in self.view.candidates:
            if not self.view.admitted_producer(candidate) or not self.view.schema_valid(candidate):
                continue
            for name in POLICY_FIELDS:
                if name not in candidate.item:
                    expected.append({"item_id": candidate.recorded_id, "field": name})
        expected.sort(key=lambda r: (utf16_key(r["item_id"]), POLICY_FIELDS.index(r["field"])))
        actual = self.trace.get("defaults_filled")
        if actual != expected:
            missing = [r for r in expected if r not in (actual or [])]
            extra = [r for r in actual or [] if r not in expected]
            return [f"defaults_filled differs from the omitted policy fields: {len(missing)} missing "
                    f"(e.g. {dumps(missing[:1])}), {len(extra)} extra (e.g. {dumps(extra[:1])})"]
        return []

    # A16 --------------------------------------------------------------------------------------------------------

    def a16(self) -> list[str]:
        out = []
        snapshot, trace = self.snapshot, self.trace
        profile, context, budget = trace.get("profile") or {}, trace.get("context") or {}, trace.get("budget") or {}
        if profile.get("id") != snapshot["profile"]["id"] or profile.get("version") != snapshot["profile"]["version"]:
            out.append("profile id or version is not the snapshot's")
        for key in ("input", "reserved_output"):
            if budget.get(key) != snapshot["budget"][key]:
                out.append(f"budget.{key} is not the snapshot's")
        if ("margin_percent" in budget) != ("margin_percent" in snapshot["budget"]) or \
                budget.get("margin_percent") != snapshot["budget"].get("margin_percent"):
            out.append("budget.margin_percent does not repeat the snapshot's")
        echo = {
            "spec": snapshot["profile"]["spec"],
            "assembly_time": snapshot["assembly_time"],
            "route_policy_version": snapshot["route_policy"]["version"],
            "tokenizer": snapshot["tokenizer"],
            "renderer": snapshot["renderer"],
        }
        for key, value in echo.items():
            if context.get(key) != value:
                out.append(f"context.{key} {context.get(key)!r} is not the snapshot's {value!r}")
        for row in self.included:
            candidate = self.view.unique(row.get("item_id"))
            if candidate is None:
                continue
            if row.get("slot") != candidate.slot:
                out.append(f"included {candidate.recorded_id!r} slot {row.get('slot')!r} is not the item's")
            if row.get("source_version") != candidate.get("source_version"):
                out.append(f"included {candidate.recorded_id!r} source_version is not the item's")
            if row.get("eligibility") != self.view.filled(candidate, "eligibility"):
                out.append(f"included {candidate.recorded_id!r} eligibility is not the item's after defaults")
        return out
