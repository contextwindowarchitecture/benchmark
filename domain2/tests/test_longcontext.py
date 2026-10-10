from __future__ import annotations

import pytest

from cwabench import gitinfo
from cwabench.contract import Contract
from cwabench2.application import evidence
from cwabench2.application.retrieval import Index, retrieve, terms
from cwabench2.application.snapshots import Settings
from cwabench2.conversations import longcontext as lq

TOKENIZER = "estimate-utf8/v1"


def corpus(ratio=1.0, budget=4096, index=0, **parameters):
    found = lq.generate(20261013, ratio, budget, index, parameters, TOKENIZER)
    found["corpus_id"] = f"lq-{lq.tier_name(ratio)}-{index:02d}"
    return found


@pytest.fixture(scope="module")
def contract(spec) -> Contract:
    return Contract(spec, gitinfo.inspect(spec).commit, allow_dirty=True)


def test_a_corpus_is_a_pure_function_of_its_inputs_and_reaches_its_size():
    assert corpus() == corpus() and corpus(index=1) != corpus()
    for ratio in (1.0, 4.0):
        found = corpus(ratio)
        assert found["tokens"] >= found["target_tokens"] == ratio * 4096
        assert found["tokens"] == sum(c["tokens"] for c in found["chunks"])
    assert [q["kind"] + "-" + q["format"] for q in corpus()["questions"]] == list(lq.QUESTIONS)
    with pytest.raises(ValueError, match="lq questions"):
        corpus(questions=["single-essay"])


def test_every_question_has_its_answer_where_the_ground_truth_says():
    found = corpus(4.0)
    bodies = {c["id"]: c["body"] for c in found["chunks"]}
    labels = [d["title"] for d in found["documents"]]
    assert len(labels) == len(set(labels))  # a site is one document
    for q in found["questions"]:
        assert set(q["needs"]) == set(q["evidence"])
        for need, sentences in q["evidence"].items():
            for sentence in sentences:  # each answer sentence is in its chunk, and in no other
                assert sentence in bodies[need] and sum(sentence in b for b in bodies.values()) == 1
        if q["format"] == "mcq":
            assert q["answer"]["kind"] == "choice" and q["question"].endswith(f"D) {lq.NOT_SAID}")
            assert q["answer"]["expected"] in q["answer"]["options"]
        if q["kind"] == "single":
            assert len(q["needs"]) == 1 and q["twin"] is not None and q["twin"] in labels
        if q["kind"] == "multi":
            assert len(q["needs"]) == 2 and q["twin"] is None
            assert len({need.split(":")[0] for need in q["needs"]}) == 2  # two documents
        if q["kind"] == "none":
            assert q["needs"] == [] and q["attributes"]["depth"] is None
            site, attribute = q["sites"][0], q["attribute"]
            stated = lq.ATTRIBUTES[attribute][1].split("{value}")[0].format(site=site)
            assert not any(stated in b for b in bodies.values())  # the site's document never states it
            twin_stated = lq.ATTRIBUTES[attribute][1].split("{value}")[0].format(site=q["twin"])
            assert any(twin_stated in b for b in bodies.values())  # but its twin does
            expected = lq.NOT_FOUND if q["format"] == "short" else "D"
            assert q["answer"]["expected"] == expected


def test_a_comparison_names_the_site_with_the_larger_value():
    for index in range(4):
        found = corpus(index=index)
        for q in (q for q in found["questions"] if q["kind"] == "multi" and q["format"] == "short"):
            values = []
            for sentences in q["evidence"].values():
                digits = "".join(ch for ch in sentences[0].split(" ")[-2] + sentences[0].split(" ")[-1]
                                 if ch.isdigit())
                values.append(int(digits))
            a, b = q["sites"]
            larger = lq.COMPARISONS[q["attribute"]][1]
            winner = a if (values[0] > values[1]) == larger else b
            assert values[0] != values[1] and q["answer"]["expected"] == winner


def test_the_retriever_ranks_by_relevance_then_id():
    found = corpus()
    index = Index(found["chunks"])
    for q in found["questions"]:
        got = retrieve(index, q["query"], 20)
        assert got[0][1] == 1.0 and len(got) <= 20
        assert got == sorted(got, key=lambda pair: (-pair[1], pair[0]))
        if q["needs"]:
            assert q["needs"][0] in [chunk_id for chunk_id, _ in got]
    assert terms("Which is the Harwell depot's code?") == ["harwell", "depot", "s", "code"]
    assert retrieve(index, "zzz qqq", 5) == []


def test_the_profiles_differ_in_the_repeated_instructions(contract):
    assert evidence.check(contract) == []
    plain, reinforced = evidence.placement(contract, False), evidence.placement(contract, True)
    assert [p["slot"] for p in reinforced].count("governance.instructions") == 2
    assert [p["slot"] for p in plain].count("governance.instructions") == 1
    repeated = {"slot": "governance.instructions", "wrap": "xml:instructions"}
    assert [p for p in reinforced if p not in plain] == [repeated]
    route = evidence.route_policy(contract)
    assert route["requires_evidence"] and route["slots"]["evidence.knowledge"]["min_relevance"] == 0.2


def test_the_prediction_admits_caps_and_sheds_from_the_lowest_rank(contract):
    found = corpus()
    q = found["questions"][0]
    by_document: dict[str, list[str]] = {}
    for c in found["chunks"]:
        by_document.setdefault(c["document"], []).append(c["id"])
    crowded = next(ids for ids in by_document.values() if len(ids) >= 2)
    # Fabricated scores: one weak chunk, and more chunks of one document than max_per_source allows
    candidates = [(crowded[0], 1.0), ("d0001:c01", 0.1)]
    others = [c["id"] for c in found["chunks"] if c["id"] not in (crowded[0], "d0001:c01")]
    candidates += [(chunk_id, round(0.9 - n / 100, 4)) for n, chunk_id in enumerate(others[:12])]
    rule = evidence.route_policy(contract)["slots"]["evidence.knowledge"]
    frozen = evidence.freeze(contract, found, q, candidates, False, Settings(margin_percent=20))
    assert frozen.excluded.get("chunk:d0001:c01") == "below_threshold"
    per_source = {}
    for entry in frozen.ranked:
        per_source[entry["source"]] = per_source.get(entry["source"], 0) + 1
    assert max(per_source.values()) <= rule["max_per_source"]
    assert [e["relevance"] for e in frozen.ranked] == sorted((e["relevance"] for e in frozen.ranked), reverse=True)
    kept = [len(frozen.expect(b).included) for b in (frozen.full, frozen.full // 2, frozen.full // 4)]
    assert kept == sorted(kept, reverse=True) and kept[0] == len(frozen.protected) + len(frozen.ranked)
    assert frozen.expect(frozen.floor - 1).refusal_reason == "protected_content_over_budget"
    assert frozen.expect(frozen.floor).refusal_reason == "evidence_required"
    best = frozen.expect(frozen.full)
    assert evidence.item_id(crowded[0]) in best.included and best.outcome == "assembled"


def test_the_baselines_keep_what_their_rule_says():
    from cwabench.canon.render import charged
    from cwabench.canon.tokenizers import TOKENIZERS
    from cwabench2.baselines import longcontext as lq_baselines

    found = corpus(4.0)
    ids = [c["id"] for c in found["chunks"]]
    index = Index(found["chunks"])
    count = TOKENIZERS[TOKENIZER]
    for q in found["questions"]:
        candidates = retrieve(index, q["query"], 64)
        full = lq_baselines.build("control-full", found, q, candidates, None, 20, TOKENIZER)
        assert full.kept == ids and full.outcome == "fits"
        assert lq_baselines.build("control-full", found, q, candidates, None, 20, TOKENIZER,
                                  context_limit=full.charged - 1).outcome == "overflow"
        truncated = lq_baselines.build("truncate-pinned", found, q, candidates, 4096, 20, TOKENIZER)
        assert truncated.kept == ids[len(ids) - len(truncated.kept):] and truncated.charged <= 4096
        assert 0 < len(truncated.kept) < len(ids)
        rag = lq_baselines.build("rag", found, q, candidates, 4096, 20, TOKENIZER)
        assert rag.kept == [chunk_id for chunk_id, _ in candidates][:len(rag.kept)] and rag.charged <= 4096
        one_more = [chunk_id for chunk_id, _ in candidates][:len(rag.kept) + 1]
        bodies = {c["id"]: c["body"] for c in found["chunks"]}
        system = lq_baselines.system_prompt(found)
        cost = charged(count(system) + count(lq_baselines.user_message([bodies[i] for i in one_more], q["question"])),
                       20)
        assert len(one_more) == len(rag.kept) or cost > 4096  # it kept the longest prefix that fits
        for built in (full, truncated, rag):
            by_record, by_text = lq_baselines.present(built, q)
            assert by_record == by_text
        if q["needs"]:
            assert all(lq_baselines.present(rag, q)[0])
    with pytest.raises(ValueError, match="no LQ baseline"):
        lq_baselines.build("window", found, found["questions"][0], [], 4096, 20, TOKENIZER)


def test_a_corpus_validates_as_its_schema():
    from cwabench2 import output

    found = corpus()
    document = {"$schema": output.schema_name("lq-corpus"), "family": "lq",
                "generator": {"name": lq.GENERATOR, "version": lq.VERSION}, "tier": "x1", "parameters": {}, **found}
    output.validate(document)
