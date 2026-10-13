"""Deterministic grading (domain-5-plan.md, 5.4): a reply's answer under the task's output contract (tasks.py), and
whether it carried out the attack in its payload (attacks.py). No judge model; nothing gates on one."""
from __future__ import annotations

from .attacks import VERDICTS, AttackGrade, judge
from .tasks import grade_reply

__all__ = ["VERDICTS", "AttackGrade", "grade_reply", "judge"]
