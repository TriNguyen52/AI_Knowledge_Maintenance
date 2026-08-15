"""Measurement invariants — frozen snapshots of all scoring constants.

These constants determine how signals are interpreted into scores.
If any of them change, assessments produced before the change are no
longer comparable to assessments produced after.  The comparability
fingerprint (fingerprint.py) uses POLICY_VERSION and SCHEMA_VERSION
to detect this.

This module exists *outside* the tests/ directory because the tests
there reference an older state of the codebase.  Invariants live inside
the package so they can be imported by fingerprint.py and verified
in CI without depending on the test suite.

Usage:
    # Programmatic check (returns list of violations, empty = OK)
    violations = verify_invariants()

    # Hard assert (raises AssertionError on any violation)
    assert_invariants()

    # CLI
    python -m ai_ready.invariants

When you intentionally change a frozen value:
    1. Update the value here in EXPECTED_VALUES
    2. Bump POLICY_VERSION (e.g. "1.0.0" -> "1.1.0")
    3. Document the change in your changelog
    The fingerprint will then correctly mark pre-change assessments
    as incomparable with post-change assessments.
"""

from __future__ import annotations

import sys
from typing import Any

# ---------------------------------------------------------------------------
# Version stamps — bump these when any frozen value changes
# ---------------------------------------------------------------------------

POLICY_VERSION = "1.0.0"
SCHEMA_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Expected (frozen) values
# ---------------------------------------------------------------------------
# Each entry is (module_path, attribute_name, expected_value).
# verify_invariants() imports the live module and checks that the attribute
# still matches.  This is deliberately a *copy* of the values — the whole
# point is that if someone changes the source constant without updating
# this file, the check fails.

EXPECTED_VALUES: list[tuple[str, str, Any]] = [
    # --- salience.py: severity weights ---
    ("ai_ready.improvement.salience", "SEVERITY_WEIGHTS", {
        "critical": 1.0,
        "high": 0.7,
        "medium": 0.4,
        "low": 0.1,
    }),
    # --- salience.py: sub-score weights ---
    ("ai_ready.improvement.salience", "W_ENCODING", 0.40),
    ("ai_ready.improvement.salience", "W_OUTCOME", 0.25),
    ("ai_ready.improvement.salience", "W_RETRIEVAL", 0.35),
    ("ai_ready.improvement.salience", "DEFAULT_SALIENCE_THRESHOLD", 0.01),
    # --- salience.py: decay parameters ---
    ("ai_ready.improvement.salience", "FAST_HALF_LIFE_DAYS", 21.0),
    ("ai_ready.improvement.salience", "SLOW_HALF_LIFE_DAYS", 180.0),
    ("ai_ready.improvement.salience", "SLOW_FLOOR_WEIGHT", 0.1),
    ("ai_ready.improvement.salience", "DECAY_EPSILON", 0.01),
    # --- pipeline.py: dimension weights ---
    ("ai_ready.pipeline", "DEFAULT_WEIGHTS", {
        "retrieval": 0.25,
        "context": 0.15,
        "consistency": 0.20,
        "trust": 0.20,
        "connectivity": 0.10,
        "workflow": 0.10,
    }),
]

# Interpretation policy entries are verified by reconstructing the
# registry and comparing every (collector_id, signal_type) entry's
# severity and score_penalty.  This avoids importing the private
# _registry dict directly.
EXPECTED_POLICY_ENTRIES: list[tuple[str, str, str, int]] = [
    # (collector_id, signal_type, severity_value, score_penalty)
    ("topic_purity", "mixed_topics", "HIGH", 15),
    ("heading_quality", "generic", "MEDIUM", 10),
    ("heading_quality", "placeholder", "CRITICAL", 20),
    ("heading_quality", "numbered_only", "CRITICAL", 20),
    ("heading_quality", "too_short", "MEDIUM", 10),
    ("context_independence", "dangling_reference", "MEDIUM", 10),
    ("context_independence", "undefined_entity", "MEDIUM", 10),
    ("link_integrity", "broken_link", "MEDIUM", 10),
    ("link_integrity", "orphan", "LOW", 5),
]


def verify_invariants() -> list[str]:
    """Check all frozen constants against their expected values.

    Returns:
        List of human-readable violation strings. Empty list means
        all invariants hold.
    """
    violations: list[str] = []

    # --- Check module-level constants ---
    for module_path, attr_name, expected in EXPECTED_VALUES:
        try:
            mod = __import__(module_path, fromlist=[attr_name])
            actual = getattr(mod, attr_name)
        except (ImportError, AttributeError) as e:
            violations.append(f"{module_path}.{attr_name}: cannot import ({e})")
            continue

        if actual != expected:
            violations.append(
                f"{module_path}.{attr_name}: expected {expected!r}, got {actual!r}"
            )

    # --- Check interpretation policy entries ---
    try:
        from ai_ready.evaluation_policy import InterpretationPolicy
        policy = InterpretationPolicy()
    except Exception as e:
        violations.append(f"InterpretationPolicy: cannot construct ({e})")
        return violations

    for collector_id, signal_type, expected_severity, expected_penalty in EXPECTED_POLICY_ENTRIES:
        entry = policy.lookup(collector_id, signal_type)
        if entry is None:
            violations.append(
                f"InterpretationPolicy({collector_id}, {signal_type}): entry missing"
            )
            continue
        if entry.severity.value != expected_severity:
            violations.append(
                f"InterpretationPolicy({collector_id}, {signal_type}): "
                f"expected severity={expected_severity}, got {entry.severity.value}"
            )
        if entry.score_penalty != expected_penalty:
            violations.append(
                f"InterpretationPolicy({collector_id}, {signal_type}): "
                f"expected score_penalty={expected_penalty}, got {entry.score_penalty}"
            )

    return violations


def assert_invariants() -> None:
    """Raise AssertionError if any invariant is violated."""
    violations = verify_invariants()
    if violations:
        msg = "Measurement invariant violations detected:\n" + "\n".join(
            f"  - {v}" for v in violations
        )
        msg += (
            "\n\nIf you intentionally changed a frozen value, update "
            "EXPECTED_VALUES / EXPECTED_POLICY_ENTRIES in "
            "ai_ready/invariants.py and bump POLICY_VERSION."
        )
        raise AssertionError(msg)


def get_versions() -> dict[str, str]:
    """Return current version stamps for fingerprinting."""
    return {
        "policy_version": POLICY_VERSION,
        "schema_version": SCHEMA_VERSION,
    }


if __name__ == "__main__":
    violations = verify_invariants()
    if violations:
        print("INVARIANT VIOLATIONS:")
        for v in violations:
            print(f"  - {v}")
        sys.exit(1)
    else:
        print(f"All invariants hold. POLICY_VERSION={POLICY_VERSION}, SCHEMA_VERSION={SCHEMA_VERSION}")
        sys.exit(0)
