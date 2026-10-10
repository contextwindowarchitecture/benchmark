"""LQ · Long-context question answering (domain-2-plan.md, 6.5): a corpus of fictional site documents, and questions
whose answer is in one chunk, in two, or in none.

**The corpus.** Each document describes one site, a name and a kind ("the Harwell depot"), and states four of six
attributes of it (its capacity, opening year, staff, manager, site code and district; five for a site a comparison
asks about), each in one sentence that names the site, among filler sentences that state no figure. Two sites may
share a name and differ in kind ("the Harwell archive"): a lexical twin, which a retriever ranks close to the other and
a model must tell apart. Documents are added until the chunked corpus reaches its tier's size, `ratio` times the run's
budget (a corpus has at least the sites its questions need, so the smallest tier can exceed it). The chunks are
Domain 1's (cwabench.producers.chunker), cut at sentence boundaries under the run's tokenizer, so every attribute
sentence lies in exactly one chunk.

**The questions**, one of each kind and format by default, ask about sites spread through the corpus, so the answer's
depth varies:

- `single`: one attribute of one site, stated once. A twin that states the same attribute with another value is
  added to the corpus, so the wrong site's value is a `distractor`;
- `multi`: which of two sites in different documents has the larger (or, for the opening year, the earlier) value;
  the answer needs both chunks;
- `none`: an attribute the site's document does not state. The right answer is NOT FOUND; a twin states it, so
  answering with the twin's value is a `distractor`.

Each is asked as `short` (the value, the site's name, or NOT FOUND) or as `mcq` (four lettered options, the last always
"The documents do not say"). A question carries what grading needs (`answer`), the chunks the fact-in-payload oracle
checks (`needs`, empty for `none`) and the sentences that carry them (`evidence`), and `query`, the question without its
options, which the retriever scores against.

Nothing here is a real place or person. A corpus is a pure function of its seed, ratio, budget and parameters.
"""
from __future__ import annotations

import hashlib
import random

from cwabench.canon.tokenizers import TOKENIZERS
from cwabench.producers.chunker import chunk
from cwabench.producers.source import Document

from .names import CODAS, ONSETS, PEOPLE

GENERATOR = "lq"
VERSION = 1

KINDS = ("depot", "archive", "clinic", "observatory", "mill", "ferry terminal", "workshop", "library")
DISTRICTS = ("Ashgrove", "Brackwater", "Cinderfell", "Duskmoor", "Eastreach", "Fallowmere", "Greyhallow", "Ironmarsh",
             "Juniper Vale", "Kestrel Point", "Larchmont", "Mistlecombe")
SURNAMES = ("Abernathy", "Blackwood", "Carrow", "Delacroix", "Everly", "Fairweather", "Galloway", "Hartigan", "Ingram",
            "Jessop", "Kincaid", "Lockhart", "Merriweather", "Northcott", "Oakes", "Penhallow", "Quarles", "Redfern",
            "Stroud", "Thorne")
CODE_LETTERS = "BCDFGHJKLMNPRSTVWXZ"

# attribute → (grading kind, the sentence stating it, the question asking it)
ATTRIBUTES = {
    "capacity": ("number", "The {site} has room for {value} crates.", "How many crates can the {site} hold?"),
    "opened": ("number", "The {site} opened in {value}.", "In what year did the {site} open?"),
    "staff": ("number", "The {site} employs {value} people.", "How many people work at the {site}?"),
    "manager": ("text", "The {site} is managed by {value}.", "Who manages the {site}?"),
    "code": ("text", "The {site} uses the site code {value}.", "What is the site code of the {site}?"),
    "district": ("text", "The {site} lies in the {value} district.", "In which district is the {site}?"),
}
STATED = 4  # attributes each document states
# attribute → (the comparison question, whether the larger value wins)
COMPARISONS = {
    "capacity": ("Which can hold more crates, the {a} or the {b}?", True),
    "staff": ("Which employs more people, the {a} or the {b}?", True),
    "opened": ("Which opened earlier, the {a} or the {b}?", False),
}
FILLER = (
    "Visitors to the {site} sign in at the front desk.",
    "The {site} closes early on public holidays.",
    "Maintenance crews inspect the roof of the {site} each spring.",
    "Deliveries to the {site} arrive through the side gate.",
    "Staff at the {site} rotate their shifts every week.",
    "The {site} keeps a spare set of keys with the night guard.",
    "A mural near the entrance of the {site} was restored last year.",
    "The car park behind the {site} floods after heavy rain.",
    "Fire drills at the {site} are announced a day in advance.",
    "The {site} shares a courier with the neighbouring sites.",
    "Most visitors reach the {site} by the river road.",
    "The heating at the {site} is switched on in early autumn.",
)
NOT_SAID = "The documents do not say"
NOT_FOUND = "NOT FOUND"
LETTERS = ("A", "B", "C", "D")
QUESTIONS = ("single-short", "single-mcq", "multi-short", "multi-mcq", "none-short", "none-mcq")

INSTRUCTIONS = ("You answer questions about a collection of documents. Use only what the documents say, and do not "
                "guess.")
OUTPUT_CONTRACT = ("Reply with the answer only: the figure, name or code asked for, with no other words. For a "
                   "multiple-choice question, reply with the letter of the right option only. If the documents do not "
                   f"give the answer, reply {NOT_FOUND}, or for a multiple-choice question the letter of the option "
                   "that says so.")


def tier_name(ratio: float) -> str:
    return f"x{ratio:g}"


def corpus_seed(seed: int, ratio: float, budget: int, index: int) -> int:
    """Each corpus's own seed: adding corpora or tiers never changes the others."""
    return int(hashlib.sha256(f"lq:{seed}:{ratio:g}:{budget}:{index}".encode()).hexdigest()[:12], 16)


def _value(rng: random.Random, attribute: str) -> str:
    if attribute == "capacity":
        return f"{rng.randint(1200, 9800):,}"
    if attribute == "opened":
        return str(rng.randint(1952, 2019))
    if attribute == "staff":
        return str(rng.randint(12, 480))
    if attribute == "manager":
        return f"{rng.choice(PEOPLE)} {rng.choice(SURNAMES)}"
    if attribute == "code":
        return f"{rng.choice(CODE_LETTERS)}{rng.choice(CODE_LETTERS)}-{rng.randint(1000, 9999)}"
    return rng.choice(DISTRICTS)


def _number(value: str) -> int:
    return int(value.replace(",", ""))


class _Site:
    def __init__(self, rng: random.Random, name: str, kind: str):
        self.name, self.kind = name, kind
        self.values = {a: _value(rng, a) for a in ATTRIBUTES}
        self.stated = set(rng.sample(sorted(ATTRIBUTES), STATED))
        self.seed = rng.getrandbits(32)  # the document's own text, so editing one site's stated set re-renders only it

    @property
    def label(self) -> str:
        return f"{self.name} {self.kind}"

    def sentence(self, attribute: str) -> str:
        return ATTRIBUTES[attribute][1].format(site=self.label, value=self.values[attribute])

    def text(self) -> str:
        """The document: a lead, then paragraphs of filler with the stated attributes' sentences among them."""
        rng = random.Random(self.seed)
        paragraphs = [[f"These notes describe the {self.label}."]]
        for _ in range(rng.randint(2, 4)):
            paragraphs.append([rng.choice(FILLER).format(site=self.label) for _ in range(rng.randint(2, 4))])
        for attribute in sorted(self.stated):
            paragraph = paragraphs[rng.randint(1, len(paragraphs) - 1)]
            paragraph.insert(rng.randint(0, len(paragraph)), self.sentence(attribute))
        return "\n\n".join(" ".join(p) for p in paragraphs)


def _new_site(rng: random.Random, used: set[tuple[str, str]], name: str | None = None) -> _Site:
    for _ in range(10000):
        pair = (name or f"{rng.choice(ONSETS)}{rng.choice(CODAS)}", rng.choice(KINDS))
        if pair not in used:
            used.add(pair)
            return _Site(rng, *pair)
    raise ValueError("no unused site left: the corpus is larger than the vocabulary allows")


def _options(rng: random.Random, right: str, wrong: list[str]) -> tuple[list[str], str]:
    """Three options in random order, the right one among them when it is not NOT_SAID, and NOT_SAID last."""
    picked = [] if right == NOT_SAID else [right]
    for value in wrong:
        if value not in picked and value != right and len(picked) < 3:
            picked.append(value)
    rng.shuffle(picked)
    options = [*picked, NOT_SAID]
    return options, LETTERS[options.index(right)]


def generate(seed: int, ratio: float, budget: int, index: int, parameters: dict, tokenizer: str) -> dict:
    """One corpus of about `ratio` × `budget` tokens and its questions."""
    kinds = list(parameters.get("questions", QUESTIONS))
    unknown = [k for k in kinds if k not in QUESTIONS]
    if unknown or not kinds:
        raise ValueError(f"lq questions must be some of {', '.join(QUESTIONS)}; got {kinds}")
    chunk_tokens = int(parameters.get("chunk_tokens", 128))
    count = TOKENIZERS[tokenizer]
    own = corpus_seed(seed, ratio, budget, index)
    rng = random.Random(own)
    target = max(1, round(ratio * budget))

    used: set[tuple[str, str]] = set()
    sites: list[_Site] = []
    tokens = 0
    needed = sum(2 if k.startswith("multi") else 1 for k in kinds) + 1  # the questions' sites, and one more
    while tokens < target or len(sites) < needed:
        site = _new_site(rng, used)
        sites.append(site)
        tokens += count(site.text())

    # The questions' sites, spread through the corpus by depth, each used once; twins are added afterwards.
    questions, taken, twins = [], set(), []
    for number, kind in enumerate(kinds, start=1):
        depth = (number - 0.5) / len(kinds)
        at = min(len(sites) - 1, int(depth * len(sites)))
        while at in taken:
            at = (at + 1) % len(sites)
        taken.add(at)
        target_site = sites[at]
        shape, form = kind.split("-")
        if shape == "multi":
            other_at = (at + len(sites) // 2) % len(sites)
            while other_at in taken or sites[other_at].name == target_site.name:
                other_at = (other_at + 1) % len(sites)
            taken.add(other_at)
            other = sites[other_at]
            attribute = rng.choice(sorted(COMPARISONS))
            while _number(other.values[attribute]) == _number(target_site.values[attribute]):
                other.values[attribute] = _value(rng, attribute)
            target_site.stated.add(attribute)
            other.stated.add(attribute)
            questions.append({"shape": shape, "format": form, "attribute": attribute, "sites": [target_site, other]})
            continue
        if shape == "single":
            attribute = rng.choice(sorted(target_site.stated))
        else:  # none: an attribute the site's document does not state
            attribute = rng.choice(sorted(set(ATTRIBUTES) - target_site.stated))
        twin = _new_site(rng, used, target_site.name)
        twin.stated.add(attribute)
        while twin.values[attribute] == target_site.values[attribute]:
            twin.values[attribute] = _value(rng, attribute)
        twins.append(twin)
        questions.append({"shape": shape, "format": form, "attribute": attribute, "sites": [target_site],
                          "twin": twin})
    for twin in twins:
        sites.insert(rng.randint(0, len(sites)), twin)

    width = max(4, len(str(len(sites))))
    documents = [Document(f"d{i + 1:0{width}d}", site.label, site.name, site.text()) for i, site in enumerate(sites)]
    doc_of = {id(site): doc for site, doc in zip(sites, documents)}
    chunks = chunk(documents, chunk_tokens, tokenizer)
    position = {c.id: n for n, c in enumerate(chunks)}

    def holder(site: _Site, attribute: str) -> str:
        sentence = site.sentence(attribute)
        found = [c.id for c in chunks if c.document == doc_of[id(site)].id and sentence in c.body]
        if len(found) != 1:
            raise AssertionError(f"{site.label}'s {attribute} sentence is in {len(found)} chunks")  # a generator defect
        return found[0]

    out = []
    for number, q in enumerate(questions, start=1):
        attribute, shape, form = q["attribute"], q["shape"], q["format"]
        grading, _, asked = ATTRIBUTES[attribute]
        site = q["sites"][0]
        twin = q.get("twin")
        others = [s.values[attribute] for s in sites if s is not site and s is not twin]
        rng.shuffle(others)
        if shape == "multi":
            a, b = q["sites"]
            stem, larger = COMPARISONS[attribute]
            stem = stem.format(a=a.label, b=b.label)
            values = (_number(a.values[attribute]), _number(b.values[attribute]))
            winner = a if (values[0] > values[1]) == larger else b
            loser = b if winner is a else a
            needs = [holder(a, attribute), holder(b, attribute)]
            evidence = {needs[0]: [a.sentence(attribute)], needs[1]: [b.sentence(attribute)]}
            if form == "short":
                answer = {"kind": "text", "expected": winner.label, "alternatives": [winner.name],
                          "distractors": [loser.label]}
            else:
                options, letter = _options(rng, winner.label, [loser.label, "They are equal"])
                answer = {"kind": "choice", "options": list(LETTERS[:len(options)]), "expected": letter}
        else:
            stem = asked.format(site=site.label)
            if shape == "single":
                right = site.values[attribute]
                needs = [holder(site, attribute)]
                evidence = {needs[0]: [site.sentence(attribute)]}
            else:
                right, needs, evidence = NOT_SAID, [], {}
            distractor = twin.values[attribute]
            if form == "short" and shape == "single":
                answer = {"kind": grading, "expected": right, "distractors": [distractor]}
                if attribute == "district":
                    answer["alternatives"] = [f"{right} district"]
            elif form == "short":
                answer = {"kind": "text", "expected": NOT_FOUND, "distractors": [distractor]}
            else:
                options, letter = _options(rng, right, [distractor, *others])
        if form == "mcq":
            text = stem + "".join(f"\n{letter_}) {option}" for letter_, option in zip(LETTERS, options))
            if shape != "multi":
                answer = {"kind": "choice", "options": list(LETTERS[:len(options)]), "expected": letter}
        else:
            text = stem
        first = min((position[c] for c in needs), default=None)
        out.append({
            "question_id": f"q{number:02d}",
            "kind": shape,
            "format": form,
            "attribute": attribute,
            "sites": [s.label for s in q["sites"]],
            "twin": twin.label if twin else None,
            "question": text,
            "query": stem,
            "answer": answer,
            "needs": needs,
            "evidence": evidence,
            "attributes": {"depth": None if first is None else round(first / len(chunks), 4)},
        })

    return {
        "seed": own,
        "ratio": ratio,
        "budget": budget,
        "target_tokens": target,
        "tokenizer": tokenizer,
        "chunk_tokens": chunk_tokens,
        "instructions": INSTRUCTIONS,
        "output_contract": OUTPUT_CONTRACT,
        "documents": [{"id": d.id, "title": d.title, "source": f"doc:{d.id}",
                       "source_version": hashlib.sha256(d.text.encode("utf-8")).hexdigest()[:12]} for d in documents],
        "chunks": [{"id": c.id, "document": c.document, "source": c.source, "source_version": c.source_version,
                    "body": c.body, "tokens": c.tokens} for c in chunks],
        "tokens": sum(c.tokens for c in chunks),
        "questions": out,
    }
