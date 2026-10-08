"""The independent primitives against the spec's definitions and its reference generator."""
from __future__ import annotations

import importlib.util
import json
import math
import random
import struct
from fractions import Fraction

import pytest

from cwabench.canon import digest, instants, jcs, strings, tokenizers
from cwabench.canon.payloads import ParseError, parse


@pytest.fixture(scope="module")
def reference(spec):
    module_spec = importlib.util.spec_from_file_location("ref_digest", spec / "conformance/generators/digest.py")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("value, text", [
    (0.0, "0"), (-0.0, "0"), (1.0, "1"), (0.5, "0.5"), (1e-7, "1e-7"), (1e-6, "0.000001"), (0.00001, "0.00001"),
    (1e21, "1e+21"), (1e20, "100000000000000000000"), (123.456, "123.456"), (-1.5e-9, "-1.5e-9"),
    (2**53 + 0.0, "9007199254740992"), (5e-324, "5e-324"), (1.7976931348623157e308, "1.7976931348623157e+308"),
])
def test_ecmascript_number_formatting(value, text):
    assert jcs.number(value) == text


def test_numbers_match_javascript_on_random_doubles():
    """Node's String(x) is ECMAScript's Number::toString itself, the ground truth RFC 8785 names."""
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    rng = random.Random(20261006)
    values = []
    while len(values) < 20000:
        value = struct.unpack("<d", rng.getrandbits(64).to_bytes(8, "little"))[0]
        if math.isfinite(value):
            values.append(value)
    # Integer-valued doubles beyond 2^53, where the spec's Python generator and ECMAScript part ways.
    values += [float(rng.randrange(2**53, 10**21)) for _ in range(2000)]
    script = ("const b=require('fs').readFileSync(0);const v=new DataView(b.buffer,b.byteOffset,b.length);"
              "const o=[];for(let i=0;i<b.length;i+=8)o.push(String(v.getFloat64(i,true)));console.log(o.join('\\n'))")
    raw = b"".join(struct.pack("<d", v) for v in values)
    expected = subprocess.run(["node", "-e", script], input=raw, capture_output=True, check=True).stdout.decode().split()
    assert [jcs.number(v) for v in values] == expected


def test_reference_generator_agrees_above_2_53(reference):
    """Regression for contextwindowarchitecture#3: the spec's generator once printed a whole double beyond 2^53 as
    its exact integer (…567168) instead of ECMAScript's shortest digits (…567000)."""
    for value in (12345678901234567890, 14904135880377241600.0, 2**53 + 2, 10**20, 123456789012345678):
        assert reference.jcs(value) == jcs.serialize(value), value


def test_integers_beyond_2_53_round_like_javascript():
    assert jcs.serialize(9007199254740993) == "9007199254740992"
    assert jcs.serialize(9007199254740991) == "9007199254740991"
    with pytest.raises(jcs.CanonicalizationError):
        jcs.serialize(10**400)


def test_strings_and_member_order():
    assert jcs.serialize("a\"\\\b\f\n\r\t\u001f /é") == '"a\\"\\\\\\b\\f\\n\\r\\t\\u001f /é"'
    assert jcs.serialize({"ｚ": 1, "\U0001F600": 2, "a": 3}) == '{"a":3,"\U0001F600":2,"ｚ":1}'
    with pytest.raises(jcs.CanonicalizationError):
        jcs.string("\ud800")


def test_digest_is_order_independent_where_producers_do_not_control_order(contract):
    case = next(c for c in contract.cases if len(json.loads(c.snapshot_bytes)["batches"]) > 1)
    snapshot = json.loads(case.snapshot_bytes)
    shuffled = json.loads(case.snapshot_bytes)
    shuffled["batches"].reverse()
    for batch in shuffled["batches"]:
        # Items without a usable id keep their order (R-2 numbers them); the others may arrive in any order.
        named = [i for i in batch["items"] if isinstance(i, dict) and strings.usable_id(i.get("id"))]
        named.reverse()
        it = iter(named)
        batch["items"] = [next(it) if isinstance(i, dict) and strings.usable_id(i.get("id")) else i for i in batch["items"]]
        batch["excluded"].reverse()
    assert digest.snapshot_digest(snapshot) == digest.snapshot_digest(shuffled) == case.expected_trace["context"]["snapshot_digest"]


def test_ecmascript_whitespace_set():
    assert strings.is_blank("﻿") and not strings.is_blank("\u001c")
    assert tokenizers.fixture_whitespace("a\u001cb c﻿d") == 3
    assert tokenizers.estimate_utf8("") == 0 and tokenizers.estimate_utf8("abcde") == 2
    assert strings.collapse("  a \t\n b　 ") == "a b"
    assert strings.collapse("é") != strings.collapse("é")  # never normalized


def test_instants_compare_at_full_precision():
    assert instants.parse("2026-09-22T11:58:00Z") == instants.parse("2026-09-22T13:58:00+02:00")
    assert instants.parse("2026-09-22T11:58:00.000Z") == instants.parse("2026-09-22T11:58:00Z")
    assert instants.parse("2026-09-22T11:58:00.0000000001Z") > instants.parse("2026-09-22T11:58:00Z")
    assert instants.parse("0000-01-01T00:00:00Z") < instants.parse("1970-01-01T00:00:00Z") == Fraction(0)
    for bad in ("2026-02-30T00:00:00Z", "2026-01-01T23:59:60Z", "2026-01-01 00:00:00Z", "2026-01-01T00:00:00+0100",
                "2026-01-01T00:00:00Z\n"):
        with pytest.raises(instants.InstantError):
            instants.parse(bad)


def test_every_expected_payload_parses(contract):
    for case in contract.cases:
        if case.expected_payload is not None:
            parsed = parse(case.snapshot["renderer"], case.expected_payload)
            assert len(parsed.occurrences) == len(case.expected_trace["included"]), case.id


@pytest.mark.parametrize("payload", [
    b'<q id="a">\nraw < bracket\n</q>\n',  # unescaped body
    b'<q id="a" conflict="g" speaker="user">\nx\n</q>\n',  # attribute order, and speaker in fixture-xml
    b'<q id="a">\nx\n</r>\n',  # wrong close
    b'<q id="a">x</q>\n',  # missing newlines
])
def test_fixture_xml_parser_is_strict(payload):
    with pytest.raises(ParseError):
        parse("fixture-xml/v1", payload)


def test_messages_payload_must_be_canonical():
    good = b'{"messages":[{"content":"","role":"user"}],"system":[],"tools":[]}'
    assert parse("cwa-messages/v1", good).occurrences == []
    with pytest.raises(ParseError):
        parse("cwa-messages/v1", b'{"system":[],"tools":[],"messages":[{"role":"user","content":""}]}')
