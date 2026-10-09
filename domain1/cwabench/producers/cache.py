"""The content-addressed variant cache (domain-1-plan.md, section 9; R-18's "SHOULD cache them by content hash").

An entry's key is the SHA-256 of the RFC 8785 serialization of its key material: the parent body's SHA-256, the
prompt id and the prompt text's SHA-256, the model, the request parameters and the sample number. Editing a parent,
the prompt or any parameter therefore changes the key, so an old variant can never be offered for new text. Entries
are immutable: a key is written once, atomically, and a later write of the same key keeps the first. Each entry
repeats its key material, and a lookup whose material differs is treated as a miss and counted as corrupt.

    <cache>/<first 2 hex>/<64 hex>.json  {"format", "key", "material", "text", "provenance"}

Replay mode reads only: a miss raises CacheMiss, which the suite reports as an error.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path

from ..canon import jcs

FORMAT = "cwa-bench-d1/summarizer-cache-entry/v1"


class CacheMiss(Exception):
    pass


def key(material: dict) -> str:
    return hashlib.sha256(jcs.serialize_bytes(material)).hexdigest()


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Cache:
    """`format` names the entries' kind; another domain's cache names its own."""

    def __init__(self, path: Path, writable: bool = True, format: str = FORMAT):
        self.path = path
        self.writable = writable
        self.format = format
        self.hits = self.misses = self.corrupt = 0
        self._lock = threading.Lock()

    def _file(self, k: str) -> Path:
        return self.path / k[:2] / f"{k}.json"

    def get(self, material: dict) -> dict | None:
        k = key(material)
        path = self._file(k)
        entry = None
        if path.is_file():
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                entry = None
            if entry is not None and (entry.get("format") != self.format or entry.get("key") != k
                                      or entry.get("material") != material or not isinstance(entry.get("text"), str)):
                with self._lock:
                    self.corrupt += 1
                entry = None
        with self._lock:
            if entry is None:
                self.misses += 1
            else:
                self.hits += 1
        return entry

    def peek(self, material: dict) -> bool:
        """Whether an entry exists, without counting a hit or a miss."""
        return self._file(key(material)).is_file()

    def put(self, material: dict, text: str, provenance: dict) -> dict:
        if not self.writable:
            raise CacheMiss("the cache is read-only in replay mode")
        k = key(material)
        entry = {"format": self.format, "key": k, "material": material, "text": text, "provenance": provenance}
        path = self._file(k)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{k[:8]}.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            try:
                os.link(temporary, path)  # fails if the key exists: the first write wins
            except FileExistsError:
                pass
        finally:
            Path(temporary).unlink(missing_ok=True)
        return json.loads(path.read_text(encoding="utf-8"))

    def entries(self) -> int:
        return sum(1 for _ in self.path.glob("*/*.json")) if self.path.is_dir() else 0
