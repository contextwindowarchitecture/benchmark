"""A content-addressed store for large artifacts: snapshots, payloads, traces (domain-1-plan.md, 12.1).

Results refer to a blob as "sha256:<hex>". blobs/index.jsonl maps each digest to its path, media type and size.
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

from . import output

EXTENSIONS = {"application/json": "json", "text/plain; charset=utf-8": "txt", "application/octet-stream": "bin"}


class BlobStore:
    def __init__(self, run_dir: Path, domain: output.Domain = output.D1):
        self.run_dir = run_dir
        self.domain = domain
        self._entries: dict[str, dict] = {}
        self._lock = threading.Lock()

    def put(self, data: bytes, media_type: str) -> str:
        digest = hashlib.sha256(data).hexdigest()
        relative = f"blobs/sha256/{digest[:2]}/{digest}.{EXTENSIONS[media_type]}"
        with self._lock:
            if digest not in self._entries:
                path = self.run_dir / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                self._entries[digest] = {
                    "$schema": self.domain.schema_name("blob"),
                    "digest": f"sha256:{digest}",
                    "path": relative,
                    "media_type": media_type,
                    "bytes": len(data),
                }
        return f"sha256:{digest}"

    def put_json(self, value) -> str:
        """Store a JSON value as written by json.dumps with sorted keys, so equal values share one blob."""
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return self.put(text.encode("utf-8"), "application/json")

    def put_text(self, data: bytes) -> str:
        return self.put(data, "text/plain; charset=utf-8")

    def __len__(self) -> int:
        return len(self._entries)

    def write_index(self) -> str:
        rows = [self._entries[d] for d in sorted(self._entries)]
        output.write_jsonl(self.run_dir / "blobs" / "index.jsonl", rows)
        return "blobs/index.jsonl"
