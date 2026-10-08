"""context.snapshot_digest (conformance/README.md, Snapshot digest): SHA-256 of the RFC 8785 serialization of the
snapshot after reordering only the arrays whose order producers do not control."""
from __future__ import annotations

import copy
import hashlib

from . import jcs
from .strings import usable_id, utf16_key


def _candidate_key(item: dict) -> tuple[bytes, bytes]:
    return utf16_key(item["id"]), jcs.serialize_bytes(item)


def normalize(snapshot: dict) -> dict:
    out = copy.deepcopy(snapshot)
    for batch in out["batches"]:
        items = batch["items"]
        named = [i for i in items if isinstance(i, dict) and usable_id(i.get("id"))]
        unnamed = [i for i in items if not (isinstance(i, dict) and usable_id(i.get("id")))]
        batch["items"] = sorted(named, key=_candidate_key) + unnamed  # unnamed keep their order: R-2 numbers them
        batch["excluded"] = sorted(batch["excluded"], key=lambda r: (utf16_key(r["item_id"]), jcs.serialize_bytes(r)))
    out["batches"] = sorted(out["batches"], key=lambda b: utf16_key(b["producer"]["id"]))
    out["conflicts"] = sorted(out["conflicts"], key=lambda g: utf16_key(g["id"]))
    for group in out["conflicts"]:
        group["items"] = sorted(group["items"], key=utf16_key)
    return out


def snapshot_digest(snapshot: dict) -> str:
    return hashlib.sha256(jcs.serialize_bytes(normalize(snapshot))).hexdigest()
