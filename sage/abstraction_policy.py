"""Leaf-layer promotion authority and frozen implementation identity."""

import hashlib
import re
from pathlib import Path

from sage.abstraction_calibrations import ACCEPTED_CALIBRATIONS

RULES = ("unsupported_identifier", "bounded_structure", "range_role")


def implementation_digest() -> str:
    """Bind publication, promotion, evidence framing and licensed repair bytes."""
    root = Path(__file__).parent
    digest = hashlib.sha256()
    for relative in (
        "abstraction_policy.py",
        "services/abstraction_publication.py",
        "services/bounded_abstraction.py",
        "adapters/abstraction_prompt.py",
        "adapters/abstraction_utils.py",
    ):
        data = (root / relative).read_bytes()
        digest.update(relative.encode() + b"\0" + str(len(data)).encode() + b"\0" + data)
    return digest.hexdigest()


def validate_promotions(classes: list[str] | tuple[str, ...]) -> dict[str, str]:
    """Refuse every unaccepted class, including settings constructed outside config."""
    if len(set(classes)) != len(classes) or any(rule not in RULES for rule in classes):
        raise ValueError("unknown or duplicate withholding class")
    refs = {}
    for rule in classes:
        accepted = ACCEPTED_CALIBRATIONS.get(rule, {})
        receipt = accepted.get("report_sha256", "")
        implementation = implementation_digest()
        if (
            not re.fullmatch(r"[0-9a-f]{64}", receipt)
            or accepted.get("implementation_sha256") != implementation
        ):
            raise ValueError("withholding class lacks accepted calibration: " + rule)
        refs[rule] = receipt
    return refs
