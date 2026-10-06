"""Compare authored intent with independent provider GET evidence.

Only two proved representation differences are normalized: omitted empty
serverMessages (the parent config-alignment evidence, 2026-09-29), and the order
of JSON Schema required members. Every other list retains its order; an owned
value disappearing, including an explicit null, is drift. Provider-added
defaults/identity/version fields are outside authored intent.
"""

from __future__ import annotations

from typing import Any


_MISSING = object()


def differences(intent: dict, actual: dict) -> list[str]:
    """Return paths whose owned values differ, without exposing private content."""
    changed: list[str] = []

    def compare(expected: Any, observed: Any, path: str, *, schema: bool = False):
        if path == "serverMessages" and expected == [] and observed is _MISSING:
            return
        if isinstance(expected, dict):
            if not isinstance(observed, dict):
                changed.append(path or "$")
                return
            for key, value in expected.items():
                compare(value, observed.get(key, _MISSING), f"{path}.{key}".lstrip("."),
                        schema=schema or key == "schema")
        elif schema and path.endswith(".required") and isinstance(expected, list):
            if not isinstance(observed, list) or sorted(expected) != sorted(observed):
                changed.append(path)
        elif type(expected) is not type(observed) or expected != observed:
            changed.append(path)

    compare(intent, actual, "")
    # Redundant legacy projections are ignored only when consistent. Conflicting
    # recording/compliance flags remain visible rather than being masked.
    for legacy, plan, field in (("recordingEnabled", "artifactPlan", "recordingEnabled"),
                                ("hipaaEnabled", "compliancePlan", "hipaaEnabled")):
        if legacy in actual and field in intent.get(plan, {}):
            value = intent[plan][field]
            if type(actual[legacy]) is not type(value) or actual[legacy] != value:
                changed.append(legacy)
    return changed
