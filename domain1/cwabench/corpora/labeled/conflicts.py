"""labeled.conflicts: instruction and fact groups, enumerated (SPEC.md R-6, R-11; conformance/README.md, Conflicts).

The expected decision for each group is written here from the README's steps, as a table generator, not taken from
any assembler or from the auditor. Every escalating configuration is built once per unresolved action (surface,
request_context, refuse), and every configuration is also built with its members' trust changed or its MCP producer
verified, which must change nothing (R-6, R-11).
"""
from __future__ import annotations

from itertools import product

from ...contract import Contract
from . import Label, excluded, kept, labeled
from .builder import Builder
from .faults import recorded_id

CORPUS = "labeled.conflicts"
RESOLUTION = {"surface": "surfaced", "request_context": "context_requested", "refuse": "refused"}
ACTIONS = ("surface", "request_context", "refuse")
POLICIES = ("governs", "defers", "escalate")

# Members an instruction group can name: (slot, authority, protected?)
KINDS = {
    "ex": ("governance.examples", "governing", False),
    "ins": ("governance.instructions", "governing", True),
    "hu": ("interaction.history", "user", False),
    "hx": ("interaction.history", "untrusted", False),  # cannot instruct
    "obs": ("evidence.tool_results", "observation", False),  # cannot instruct
}


def instruction_decision(members: list[tuple[str, str, str, bool]]):
    """members: (id, authority, conflict_policy, protected), admitted ones only. Returns (decided_by, winner,
    excluded ids) or ("escalated", None, []) — following conformance/README.md, Conflicts, instruction groups."""
    if len(members) < 2:
        return "moot", None, []
    instructing = [m for m in members if m[1] in ("governing", "user")]
    top = "governing" if any(m[1] == "governing" for m in instructing) else "user"
    peers = [m for m in instructing if m[1] == top]
    if len(peers) <= 1:
        return "authority", peers[0][0] if peers else None, []
    policies = [m[2] for m in peers]
    if policies.count("governs") == 1 and policies.count("defers") == len(peers) - 1:
        winner = peers[policies.index("governs")]
        losers = [m for m in peers if m is not winner]
        if any(m[3] for m in losers):
            return "escalated", None, []
        return "policy", winner[0], sorted(m[0] for m in losers)
    return "escalated", None, []


def _label(b: Builder, groups: dict[str, tuple], action_of: dict[str, str], fates: dict, note: str) -> Label:
    """groups: id → (decided_by, winner, excluded, kind). Works out the conflict records, the exclusions and any
    conflict_unresolved refusal."""
    label = Label(fates={recorded_id(b, i): kept() for i in b.items()}, notes=note)
    label.fates.update(fates)
    blocking = []
    for group_id, (decided, winner, losers, kind) in groups.items():
        if decided == "escalated":
            resolution = RESOLUTION[action_of[group_id]]
            if resolution != "surfaced":
                blocking.append(resolution)
            label.conflicts[group_id] = {"decided_by": "escalated", "resolution": resolution, "winner": None}
        elif decided == "moot":
            label.conflicts[group_id] = {"decided_by": "moot", "resolution": "moot", "winner": None}
        else:
            label.conflicts[group_id] = {"decided_by": decided, "resolution": "resolved", "winner": winner}
            for loser in losers:
                label.fates[loser] = excluded("conflict_deferred" if kind == "instruction" else "conflict_lost",
                                              slot=b.find(loser)["slot"])
    if blocking:
        label.outcome, label.refusal = "refused", "conflict_unresolved"
        label.recovery = "request_context" if all(r == "context_requested" for r in blocking) else None
    return label


def _instruction_case(contract, name, spec, action, trust=None):
    """spec: list of (kind, policy). Builds one instruction group of those members."""
    b = Builder(contract, name)
    b.base()
    b.route["on_unresolved_instruction"] = action
    members, ids = [], []
    for n, (kind, policy) in enumerate(spec):
        slot, authority, protected = KINDS[kind]
        item_id = f"{kind}:{n}"
        fields = {"authority": authority, "conflict_policy": policy}
        if trust and slot == "interaction.history":
            fields["trust"] = trust
        b.item(item_id, slot, **fields)
        members.append((item_id, authority, policy, protected))
        ids.append(item_id)
    b.group("g:i", "instruction", ids)
    decided, winner, losers = instruction_decision(members)
    label = _label(b, {"g:i": (decided, winner, losers, "instruction")}, {"g:i": action}, {},
                   f"instruction group {spec}, on_unresolved_instruction {action}: {decided}")
    return labeled(CORPUS, name, b.bytes(), label, ("R-6", "R-11"))


def _fact_case(contract, name, members, policy, *, tier_kb=None, verified=False, trust=None):
    """members: list of (id, producer, slot, freshness, scope). policy: the route's facts entry."""
    b = Builder(contract, name)
    b.base()
    b.route["producers"]["kb2"] = {"kind": "retrieval", "slots": ["evidence.knowledge"]}
    if verified:
        b.route["producers"]["tools"]["verified"] = True
    if tier_kb:
        b.route["tier_upgrades"] = {"evidence.knowledge": tier_kb}
    b.route["facts"] = {"price": policy}
    for item_id, producer, slot, freshness, scope in members:
        fields = {"freshness": freshness, "scope": scope}
        if trust:
            fields["trust"] = trust
        b.item(item_id, slot, producer=producer, **fields)
    b.group("f:price", "fact", [m[0] for m in members], fact="price")

    # conformance/README.md, Conflicts, fact groups.
    eligible = [m for m in members if m[1] in policy["precedence"] and all(k in m[4] for k in policy.get("scope", []))]
    decided, winner = "escalated", None
    if eligible:
        best = min(policy["precedence"].index(m[1]) for m in eligible)
        leaders = [m for m in eligible if policy["precedence"].index(m[1]) == best]
        if len(leaders) == 1:
            decided, winner = "policy", leaders[0][0]
        elif policy.get("freshness_tiebreak"):
            latest = max(m[3] for m in leaders)  # comparable: every freshness here is a Z-time of one format
            if sum(m[3] == latest for m in leaders) == 1:
                decided, winner = "freshness", next(m[0] for m in leaders if m[3] == latest)
    losers = sorted(m[0] for m in members if m[0] != winner) if winner else []
    protected = tier_kb == "protected"
    if winner and protected and any(b.find(l)["slot"] == "evidence.knowledge" for l in losers):
        decided, winner, losers = "escalated", None, []
    label = _label(b, {"f:price": (decided, winner, losers, "fact")}, {"f:price": policy["on_unresolved"]}, {},
                   f"fact group, precedence {policy['precedence']}: {decided}")
    return labeled(CORPUS, name, b.bytes(), label, ("R-6", "R-11"))


def build(contract: Contract):
    out = []
    configs = [[("ex", p1), ("ex", p2)] for p1, p2 in product(POLICIES, repeat=2)]
    configs += [[("ex", p1), ("ex", p2), ("ex", p3)] for p1, p2, p3 in product(POLICIES, repeat=3)]
    configs += [[("hu", p1), ("hu", p2)] for p1, p2 in product(POLICIES, repeat=2)]
    configs += [
        [("ex", "defers"), ("hu", "governs")],  # one governing peer; the user member stays
        [("ex", "governs"), ("ex", "defers"), ("hu", "defers")],  # user below governing peers stays
        [("ex", "governs"), ("ex", "defers"), ("hx", "governs"), ("obs", "governs")],  # non-instructing stay
        [("ins", "governs"), ("ex", "defers")],  # the deferring peer is not protected
        [("ins", "defers"), ("ex", "governs")],  # excluding a protected peer escalates
        [("ins", "governs"), ("ins", "defers")],
        [("hx", "governs"), ("obs", "governs")],  # nobody can instruct: decided by authority, no winner
        [("hx", "governs"), ("hu", "defers")],  # one user peer
    ]
    for n, spec in enumerate(configs):
        members = [(f"{k}:{i}", KINDS[k][1], p, KINDS[k][2]) for i, (k, p) in enumerate(spec)]
        escalates = instruction_decision(members)[0] == "escalated"
        for action in ACTIONS if escalates else ("refuse",):
            tag = "-".join(f"{k}.{p[0]}" for k, p in spec)
            out.append(_instruction_case(contract, f"instruction-{n:02d}-{tag}-{action}", spec, action))
        if any(k in ("hu", "hx") for k, _ in spec):
            for trust in ("verified", "untrusted"):
                tag = "-".join(f"{k}.{p[0]}" for k, p in spec)
                out.append(_instruction_case(contract, f"instruction-{n:02d}-{tag}-trust-{trust}", spec, "refuse",
                                             trust))

    # A group left with one admitted member is moot, and excludes nothing.
    b = Builder(contract, "instruction-moot")
    b.base()
    b.item("ex:0", "governance.examples", conflict_policy="governs")
    b.item("ex:1", "governance.examples", conflict_policy="governs", revoked_by="turn:0")
    b.group("g:i", "instruction", ["ex:0", "ex:1"])
    out.append(labeled(CORPUS, "instruction-moot", b.bytes(), _label(
        b, {"g:i": ("moot", None, [], "instruction")}, {"g:i": "refuse"},
        {"ex:1": excluded("revoked", slot="governance.examples")}, "one admitted member: moot"), ("R-11",)))

    # Two escalating groups: request_context only when every blocking group asked for it (R-11, R-12).
    for actions in (("request_context", "request_context"), ("request_context", "refuse"), ("surface", "refuse")):
        name = f"instruction-two-groups-{actions[0]}-{actions[1]}"
        b = Builder(contract, name)
        b.base()
        for g in (0, 1):
            b.item(f"ex:{g}a", "governance.examples", conflict_policy="governs")
            b.item(f"ex:{g}b", "governance.examples", conflict_policy="governs")
            b.group(f"g:{g}", "instruction", [f"ex:{g}a", f"ex:{g}b"])
        # Both groups share the route's one action, so the second group's differs through a fact group instead.
        b.route["on_unresolved_instruction"] = actions[0]
        b.route["facts"] = {"f": {"precedence": ["nobody"], "on_unresolved": actions[1]}}
        b.item("kb:f1", "evidence.knowledge")
        b.item("kb:f2", "evidence.knowledge")
        b.group("g:f", "fact", ["kb:f1", "kb:f2"], fact="f")
        groups = {"g:0": ("escalated", None, [], "instruction"), "g:1": ("escalated", None, [], "instruction"),
                  "g:f": ("escalated", None, [], "fact")}
        out.append(labeled(CORPUS, name, b.bytes(), _label(
            b, groups, {"g:0": actions[0], "g:1": actions[0], "g:f": actions[1]}, {},
            f"three escalating groups ({actions[0]}, {actions[0]}, {actions[1]})"), ("R-11", "R-12")))

    t_old, t_new = "2026-09-22T11:50:00Z", "2026-09-22T11:59:00Z"
    acme, acme_user = {"tenant": "acme"}, {"tenant": "acme", "user": "u_1"}
    facts = [
        ("precedence-first-wins", [("kb:f", "kb", "evidence.knowledge", t_old, acme),
                                   ("obs:f", "tools", "evidence.tool_results", t_new, acme)],
         {"precedence": ["kb", "tools"]}, {}),
        ("precedence-reversed", [("kb:f", "kb", "evidence.knowledge", t_new, acme),
                                 ("obs:f", "tools", "evidence.tool_results", t_old, acme)],
         {"precedence": ["tools", "kb"]}, {}),
        ("unlisted-producer-loses", [("kb:f", "kb", "evidence.knowledge", t_old, acme),
                                     ("obs:f", "tools", "evidence.tool_results", t_new, acme)],
         {"precedence": ["kb"]}, {}),
        ("nobody-eligible", [("kb:f", "kb", "evidence.knowledge", t_old, acme),
                             ("obs:f", "tools", "evidence.tool_results", t_new, acme)],
         {"precedence": ["kb2"]}, {}),
        ("scope-key-required", [("kb:f", "kb", "evidence.knowledge", t_new, acme),
                                ("obs:f", "tools", "evidence.tool_results", t_old, acme_user)],
         {"precedence": ["kb", "tools"], "scope": ["user"]}, {}),
        ("leaders-tie-no-tiebreak", [("kb:f1", "kb", "evidence.knowledge", t_old, acme),
                                     ("kb:f2", "kb", "evidence.knowledge", t_new, acme)],
         {"precedence": ["kb"]}, {}),
        ("leaders-tiebreak-newer", [("kb:f1", "kb", "evidence.knowledge", t_old, acme),
                                    ("kb:f2", "kb", "evidence.knowledge", t_new, acme),
                                    ("obs:f", "tools", "evidence.tool_results", t_new, acme)],
         {"precedence": ["kb", "tools"], "freshness_tiebreak": True}, {}),
        ("leaders-tiebreak-equal", [("kb:f1", "kb", "evidence.knowledge", t_new, acme),
                                    ("kb:f2", "kb", "evidence.knowledge", t_new, acme)],
         {"precedence": ["kb"], "freshness_tiebreak": True}, {}),
        ("freshness-never-beats-precedence", [("kb:f", "kb", "evidence.knowledge", t_old, acme),
                                              ("obs:f", "tools", "evidence.tool_results", t_new, acme)],
         {"precedence": ["kb", "tools"], "freshness_tiebreak": True}, {}),
        ("tiebreak-cannot-make-ineligible-win", [("kb2:f", "kb2", "evidence.knowledge", t_new, acme),
                                                 ("kb:f", "kb", "evidence.knowledge", t_old, acme)],
         {"precedence": ["kb"], "freshness_tiebreak": True}, {}),
        ("protected-loser-escalates", [("kb:f", "kb", "evidence.knowledge", t_old, acme),
                                       ("obs:f", "tools", "evidence.tool_results", t_new, acme)],
         {"precedence": ["tools", "kb"]}, {"tier_kb": "protected"}),
        ("verified-flag-ignored", [("kb:f", "kb", "evidence.knowledge", t_old, acme),
                                   ("obs:f", "tools", "evidence.tool_results", t_new, acme)],
         {"precedence": ["kb", "tools"]}, {"verified": True}),
        ("trust-ignored", [("kb:f", "kb", "evidence.knowledge", t_old, acme),
                           ("obs:f", "tools", "evidence.tool_results", t_new, acme)],
         {"precedence": ["tools", "kb"]}, {"trust": "verified"}),
    ]
    for name, members, policy, options in facts:
        for action in ACTIONS:
            out.append(_fact_case(contract, f"fact-{name}-{action}", members,
                                  {**policy, "on_unresolved": action}, **options))
    return out
