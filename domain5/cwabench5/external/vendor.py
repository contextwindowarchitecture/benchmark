"""Copying the external sets into the repository (domain-5-plan.md, 6.6): a seeded sample of each, from its source at
a pinned revision, with a SHA-256 lock.

`vendor(source, target)` reads the source files from `source/sep/` and `source/llmail/` (downloaded as the README
says), refuses any whose SHA-256 differs from SOURCES, and writes `target/`: each set's sample, its licence and notice,
and `lock.json`, which pins the sources and the SHA-256 of every file written. `check(target)` re-reads the written
files against the lock; S0 runs it. Sampling is deterministic: the same sources give the same bytes.

- **SEP**: SEP_CASES cases drawn without replacement by SEP_SEED, in draw order; the first tenth is the pilot. Each
  case keeps SEP's fields, and its probe (the text SEP appended to the data, which is also what it appended to the
  instruction) is taken out of `prompt_instructed` for the attack-in-payload oracle.
- **LLMail-Inject**: both phases' raw submissions, deduplicated by subject and body; a text succeeded at its original
  level if any of its submissions set every objective flag. Per scenario, up to SUCCESSES successful texts and the
  rest failed ones to PER_SCENARIO, each stratum drawn by LLMAIL_SEED; the first tenth of each stratum (rounded up) is
  the pilot. In scenario 4, texts holding the figure to exfiltrate are left out, as the challenge filtered them. Each
  submission gets its scenario's email context from `scenarios.json` (llmail.py).
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import re
from pathlib import Path

from .. import output

SEP_SEED, SEP_CASES = 20261013, 1000
LLMAIL_SEED, PER_SCENARIO, SUCCESSES = 20261013, 500, 250
PILOT_SHARE = 10  # the pilot is the first tenth

SOURCES = {
    "sep": {
        "name": "SEP (Should it be Executed or Processed?)",
        "repository": "https://github.com/egozverev/Should-It-Be-Executed-Or-Processed",
        "revision": "7606c0696f20f5aa433169fd2221f76852d1d4f5",
        "licence": "Code MIT (LICENCE); the dataset CC BY 4.0 (readme.md, License)",
        "citation": "Zverev, Abdelnabi, Tabesh, Fritz and Lampert, Can LLMs Separate Instructions From Data? And What "
                    "Do We Even Mean By That?, ICLR 2025",
        "files": {
            "SEP_dataset.json": ("SEP_dataset/SEP_dataset.json",
                                 "9f81a52ba089073251793f8499fbb79bb4112e94cf48d31b7f8805e8b44fa3ce"),
            "LICENCE": ("LICENCE", "39f3091693dc0df89e04550e140f52a8a1c5cea446e88393401181060c18073c"),
            "readme.md": ("readme.md", "8825244adbc299be26ed32e957bc9d3f48c8a9036b4ff28a8f5eddaa140ee4fe"),
        },
    },
    "llmail": {
        "name": "LLMail-Inject: Adaptive Prompt Injection Challenge",
        "repository": "https://huggingface.co/datasets/microsoft/llmail-inject-challenge",
        "revision": "1063bdf01ec8762b812d5e06ee768a06faa5a6f7",
        "licence": "MIT (the dataset card's license field)",
        "citation": "Abdelnabi et al., LLMail-Inject: A Dataset from a Realistic Adaptive Prompt Injection Challenge, "
                    "arXiv 2506.09956",
        "files": {
            "raw_submissions_phase1.jsonl": ("data/raw_submissions_phase1.jsonl",
                                             "a9c62eca699dd270fdfbbfbfcc1253f5e5017f6d5a34ff7cb0f2cbb80b7f7c0a"),
            "raw_submissions_phase2.jsonl": ("data/raw_submissions_phase2.jsonl",
                                             "a9207e1d893ccb74ca6f9cc5eecea433bc49c23a26bed88088afd385c7ab18b6"),
            "scenarios.json": ("data/scenarios.json",
                               "3d7c55fd04826dd29fa11b9f9912ae270fffb16a6c52a7eb2bff00457d378e4f"),
            "README.md": ("README.md", "d85767e349ab1abbaf43db9390c4e906e34954e5df3a1335c8f1516efa64e812"),
        },
    },
}
FIGURE = re.compile(r"9\.378")  # scenario 4's profit forecast, $9.378 million (scenarios.json)
NOTICE = """# {name}

Vendored by `cwabench --domain 5 external vendor` (cwabench5/external/vendor.py) from {repository} at {revision}.

Licence: {licence}. {extra}

Cite: {citation}.

`{sample}` is a seeded sample of the source, described in vendor.py; `lock.json` one level up pins the source files and
the SHA-256 of every file here.
"""
EXTRA = {
    "sep": "The readme places the dataset under CC BY 4.0 (it names a `data/` folder; the dataset is in `SEP_dataset/` "
           "and `datasets/`), and the repository's MIT licence, copied here as LICENCE, covers it either way. The "
           "cases are unchanged; the `probe` field is derived from them.",
    "llmail": "The submissions' subjects and bodies are unchanged; each carries its level, phase, team and whether it "
              "succeeded there, and the context it is placed in. The README is copied as LICENCE-README.md for the "
              "licence and terms it states.",
}


class VendorError(Exception):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _sources(source: Path, name: str) -> dict[str, Path]:
    found = {}
    for local, (_, sha) in SOURCES[name]["files"].items():
        path = source / name / local
        if not path.is_file():
            raise VendorError(f"{path} is missing")
        if _sha256(path) != sha:
            raise VendorError(f"{path} is not the pinned file (SHA-256 differs)")
        found[local] = path
    return found


def probe_of(case: dict) -> str:
    """The text SEP appended to the data: `prompt_instructed` is the clean data and the probe joined by a space."""
    clean, instructed = case["prompt_clean"], case["prompt_instructed"]
    if instructed.startswith(clean + " "):
        return instructed[len(clean) + 1:]
    if instructed.endswith(" " + clean):
        return instructed[:-len(clean) - 1]
    raise VendorError(f"cannot find the probe in SEP case {case.get('case_id')}")


def sep_sample(dataset: list[dict]) -> list[dict]:
    rng = random.Random(SEP_SEED)
    pilot = math.ceil(SEP_CASES / PILOT_SHARE)
    cases = []
    for rank, index in enumerate(rng.sample(range(len(dataset)), SEP_CASES)):
        case = {"case_id": f"sep-{index:04d}", "index": index, "pilot": rank < pilot,
                **{k: dataset[index][k] for k in ("system_prompt_clean", "system_prompt_instructed", "prompt_clean",
                                                  "prompt_instructed", "witness", "info")}}
        case["probe"] = probe_of(case)
        cases.append(case)
    return cases


def llmail_text(subject: str, body: str) -> str:
    """An email as the challenge handed it to the model (README, Labelled_unique_submissions)."""
    return f"Subject of the email: {subject}.   Body: {body}"


def llmail_unique(paths: list[Path]) -> dict[tuple[str, str], dict]:
    """Every distinct (subject, body), first submission first, with whether any submission of it succeeded."""
    found: dict[tuple[str, str], dict] = {}
    for phase, path in enumerate(paths, 1):
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                row = json.loads(line)
                flags = json.loads(row["objectives"])
                key = (row["subject"], row["body"])
                ok = bool(flags) and all(flags.values())
                if key in found:
                    found[key]["succeeded"] = found[key]["succeeded"] or ok
                    found[key]["submissions"] += 1
                    continue
                found[key] = {"source_id": row["job_id"], "phase": phase, "level": row["scenario"],
                              "scenario": int(row["scenario"][5]), "team": row["team_id"], "succeeded": ok,
                              "submissions": 1}
    return found


def llmail_sample(unique: dict[tuple[str, str], dict], scenarios: dict) -> tuple[list[dict], dict]:
    from .llmail import context  # the scenario contexts

    rng = random.Random(LLMAIL_SEED)
    strata: dict[tuple[int, bool], list] = {(s, ok): [] for s in (1, 2, 3, 4) for ok in (True, False)}
    left_out = {"not_utf8": 0, "holds_the_figure": 0}
    for (subject, body), meta in sorted(unique.items(), key=lambda kv: kv[1]["source_id"]):
        text = llmail_text(subject, body)
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            left_out["not_utf8"] += 1
            continue
        if meta["scenario"] == 4 and FIGURE.search(text):
            left_out["holds_the_figure"] += 1
            continue
        strata[(meta["scenario"], meta["succeeded"])].append((subject, body, meta))
    cases, counts = [], {}
    for scenario in (1, 2, 3, 4):
        won = min(SUCCESSES, len(strata[(scenario, True)]))
        for ok, take in ((True, won), (False, PER_SCENARIO - won)):
            pool = strata[(scenario, ok)]
            if len(pool) < take:
                raise VendorError(f"scenario {scenario}: {len(pool)} {'successful' if ok else 'failed'} texts, "
                                  f"{take} needed")
            chosen = rng.sample(pool, take)
            pilot = math.ceil(take / PILOT_SHARE)
            counts[f"scenario {scenario}, {'succeeded' if ok else 'failed'}"] = {"available": len(pool),
                                                                                 "taken": take, "pilot": pilot}
            for rank, (subject, body, meta) in enumerate(chosen):
                case = {"case_id": f"llmail-{meta['source_id']}", **meta, "pilot": rank < pilot,
                        "subject": subject, "body": body}
                case["context"] = context(case, scenarios, rng)
                cases.append(case)
    return cases, {"strata": counts, "left_out": left_out, "unique_texts": len(unique)}


def _write(target: Path, relative: str, data: bytes, written: dict[str, str]) -> None:
    path = target / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    written[relative] = hashlib.sha256(data).hexdigest()


def _json(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=1) + "\n").encode("utf-8")


def vendor(source: Path, target: Path, log=print) -> dict:
    written: dict[str, str] = {}
    sep_files = _sources(source, "sep")
    log("sep: sampling")
    cases = sep_sample(json.loads(sep_files["SEP_dataset.json"].read_text(encoding="utf-8")))
    _write(target, "sep/cases.json", _json(cases), written)
    _write(target, "sep/LICENCE", sep_files["LICENCE"].read_bytes(), written)
    llmail_files = _sources(source, "llmail")
    log("llmail: reading both phases")
    unique = llmail_unique([llmail_files["raw_submissions_phase1.jsonl"], llmail_files["raw_submissions_phase2.jsonl"]])
    scenarios_raw = llmail_files["scenarios.json"].read_bytes()
    submissions, described = llmail_sample(unique, json.loads(scenarios_raw))
    _write(target, "llmail/submissions.json", _json(submissions), written)
    _write(target, "llmail/scenarios.json", scenarios_raw, written)
    _write(target, "llmail/LICENCE-README.md", llmail_files["README.md"].read_bytes(), written)
    for name, sample in (("sep", "cases.json"), ("llmail", "submissions.json")):
        meta = SOURCES[name]
        _write(target, f"{name}/NOTICE.md", NOTICE.format(**meta, extra=EXTRA[name], sample=sample).encode(), written)
    lock = {
        "$schema": output.schema_name("external-lock"),
        "sets": {
            "sep": {**{k: v for k, v in SOURCES["sep"].items() if k != "files"},
                    "sources": {local: {"path": p, "sha256": s} for local, (p, s) in SOURCES["sep"]["files"].items()},
                    "sample": {"seed": SEP_SEED, "cases": SEP_CASES, "pilot": math.ceil(SEP_CASES / PILOT_SHARE),
                               "of": len(json.loads(sep_files["SEP_dataset.json"].read_text(encoding="utf-8")))}},
            "llmail": {**{k: v for k, v in SOURCES["llmail"].items() if k != "files"},
                       "sources": {local: {"path": p, "sha256": s}
                                   for local, (p, s) in SOURCES["llmail"]["files"].items()},
                       "sample": {"seed": LLMAIL_SEED, "per_scenario": PER_SCENARIO, "successes": SUCCESSES,
                                  "cases": len(submissions), "pilot": sum(c["pilot"] for c in submissions),
                                  **described}},
        },
        "files": dict(sorted(written.items())),
    }
    output.validate(lock)
    (target / "lock.json").write_bytes(_json(lock))
    return lock


def check(target: Path) -> list[str]:
    """Problems with the vendored files: missing, changed, or not in the lock."""
    lock_path = target / "lock.json"
    if not lock_path.is_file():
        return [f"{lock_path} is missing"]
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    problems = []
    for relative, sha in lock["files"].items():
        path = target / relative
        if not path.is_file():
            problems.append(f"{relative} is missing")
        elif _sha256(path) != sha:
            problems.append(f"{relative} differs from the lock")
    present = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()} - {"lock.json"}
    problems += [f"{p} is not in the lock" for p in sorted(present - set(lock["files"]))]
    return problems
