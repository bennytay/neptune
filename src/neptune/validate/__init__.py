"""Ingestion integrity and data quality: deterministic rules over a stored package (ADR 0054).

``validate_package(read_package(root))`` runs every rule whose inputs are on main and returns a
``ValidationReport``: findings from the validator's transform, and each rule's coverage. The job
runs it in its ``validate`` phase and adds the findings to the package, so the receipt lists them.
"""

from neptune.validate.engine import (
    FINDINGS_CAPPED,
    RULE_FAILED,
    VALIDATOR_ID,
    VALIDATOR_VERSION,
    Bounds,
    Inputs,
    Omitted,
    Rule,
    RuleOutcome,
    ValidationReport,
    validate_package,
    validator_transform,
)
from neptune.validate.rules import ALL_RULES, DEFAULT_RULES

__all__ = [
    "ALL_RULES",
    "DEFAULT_RULES",
    "FINDINGS_CAPPED",
    "RULE_FAILED",
    "VALIDATOR_ID",
    "VALIDATOR_VERSION",
    "Bounds",
    "Inputs",
    "Omitted",
    "Rule",
    "RuleOutcome",
    "ValidationReport",
    "validate_package",
    "validator_transform",
]
