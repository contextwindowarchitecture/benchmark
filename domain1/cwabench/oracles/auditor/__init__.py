"""The trace auditor (domain-1-plan.md, section 6): invariants checked from the snapshot, the trace and the payload
alone, so it can judge any assembly, including ones no expected output exists for. It never assembles.

    result = audit(contract, snapshot_bytes, payload_or_none, trace)
    result.status        "pass" or "fail"
    result.checks        {"A1": CheckResult, ...}
"""
from __future__ import annotations

from .checks import CHECKS, Audit, AuditResult, CheckResult


def audit(contract, snapshot_bytes: bytes, payload: bytes | None, trace: dict) -> AuditResult:
    return Audit(contract, snapshot_bytes, payload, trace).run()


__all__ = ["CHECKS", "AuditResult", "CheckResult", "audit"]
