"""Attacks: seeded templates with code-graded success, written into task instances (domain-5-plan.md, 6.2 to 6.8).

families.py holds the templates, carriers.py how an instruction is dressed, compose.py how a probe is made, and
check.py S0's independent check of a probe.
"""
from __future__ import annotations

from .carriers import CARRIERS
from .compose import POSITIONS, attacked_task, compose
from .families import FAMILIES, Template, every_template, templates

__all__ = ["CARRIERS", "FAMILIES", "POSITIONS", "Template", "attacked_task", "compose", "every_template",
           "templates"]
