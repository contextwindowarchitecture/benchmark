"""A synthetic source corpus about fictional organizations, built from a seed (domain-1-plan.md, section 9).

Each document is a few paragraphs of operational prose about one fictional company and its project, dense with the
things the fidelity checks follow: amounts, percentages, ISO dates, ticket and invoice ids, URLs under `.example`,
and multi-word names. Some sentences already carry an imperative ("you must …"), so a faithful summary may keep one
and the marker check has to compare against the parent. Every name is invented; none refers to a real entity.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

COMPANIES = (
    ("Brightwater Logistics", "brightwater", "Project Halcyon", "Port Esk"),
    ("Corvane Systems", "corvane", "Northgate Ledger", "Kell Harbor"),
    ("Halden Mutual", "halden", "Vesper Relay", "Dunmore Yard"),
    ("Tessaly Labs", "tessaly", "Project Lantern", "Arden Quay"),
    ("Orrin Freight", "orrin", "Saltmarsh Gateway", "Fennick Basin"),
    ("Marrow Point Clinic", "marrow", "Cedar Intake", "Lowell Crossing"),
    ("Quillon Energy", "quillon", "Project Ember", "Stray Point"),
    ("Abbot Riverworks", "abbot", "Tidewell Exchange", "Harrow Bend"),
)
PEOPLE = ("Ines Varga", "Tobias Rendt", "Maren Okafor", "Lucan Pryce", "Sefa Lindqvist", "Odile Marchetti",
          "Rafe Tanaka", "Priya Holloway", "Jonas Ebbe", "Wren Castellan")
TEAMS = ("the platform team", "the billing desk", "the field operations group", "the audit committee",
         "the integration squad", "the night shift")
SYSTEMS = ("the dispatch queue", "the settlement service", "the intake portal", "the routing table",
           "the reconciliation job", "the archive bucket")
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")


@dataclass(frozen=True)
class Document:
    id: str
    title: str
    company: str
    text: str  # paragraphs separated by one blank line


def _date(rng: random.Random) -> str:
    return f"2026-{rng.randint(1, 9):02d}-{rng.randint(1, 28):02d}"


def _sentence(rng: random.Random, company: str, slug: str, project: str, place: str) -> str:
    person, other = rng.sample(PEOPLE, 2)
    team, system = rng.choice(TEAMS), rng.choice(SYSTEMS)
    n = rng.randint(120, 9800)
    pct = rng.randint(2, 48)
    money = f"${rng.randint(11, 940)},{rng.randint(0, 999):03d}"
    ticket = f"{slug[:2].upper()}-{rng.randint(1000, 9999)}"
    invoice = f"INV-{rng.randint(10000, 99999)}"
    date, later = sorted((_date(rng), _date(rng)))
    url = f"https://docs.{slug}.example/{rng.choice(('runbooks', 'reports', 'policies'))}/{project.split()[-1].lower()}"
    templates = (
        f"{company} moved {n:,} pallets through {place} on {date}, up {pct}% from the previous month.",
        f"{person} approved ticket {ticket} on {date} to extend {project} until {later}.",
        f"The runbook for {system} lives at {url} and was last revised on {date}.",
        f"Invoice {invoice} for {money} was disputed by {team} because the rate differed from the {date} quote.",
        (f"{person} and {other} agreed that {system} would pause for {rng.randint(2, 9)} hours during the {place} "
         "cutover."),
        f"Latency in {system} fell by {pct}% after {team} replaced the batch window on {date}.",
        f"You must file ticket {ticket} before any change to {system} in {place}, as {person} wrote on {date}.",
        f"{project} served {n:,} requests in its first week, of which {pct}% came from {place}.",
        f"{team.capitalize()} reported {rng.randint(2, 40)} failed transfers on {date}, all traced to {system}.",
        f"The budget for {project} is {money}, and {person} owns the remaining {pct}% of the rollout.",
        f"{company} will retire {system} on {later}; {person} has the migration plan at {url}.",
        f"Operators must never restart {system} while {project} is replaying the {date} backlog.",
        f"{other} measured {n:,} records in {system} on {date}, against {n + rng.randint(5, 400):,} expected.",
        f"A review on {date} found that {team} had waived {rng.randint(2, 30)} checks for {project} without a ticket.",
    )
    return rng.choice(templates)


def documents(seed: int, count: int, paragraphs: tuple[int, int] = (4, 6),
              sentences: tuple[int, int] = (3, 6)) -> list[Document]:
    """`count` documents, each a pure function of `seed` and its position."""
    out = []
    for index in range(count):
        rng = random.Random(f"{seed}:{index}")
        company, slug, project, place = COMPANIES[index % len(COMPANIES)]
        lead = (f"{company} operates {project} out of {place}. This note summarizes its status as of "
                f"{_date(rng)} for {rng.choice(TEAMS)}.")
        paras = [lead]
        for _ in range(rng.randint(*paragraphs)):
            paras.append(" ".join(_sentence(rng, company, slug, project, place)
                                  for _ in range(rng.randint(*sentences))))
        out.append(Document(f"doc-{index + 1:02d}", f"{company}: {project}", company, "\n\n".join(paras)))
    return out
