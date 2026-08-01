"""Comparability fingerprint for knowledge assessments.

A fingerprint captures the measurement conditions under which an
assessment was produced: policy version, schema version, and the
set of collectors (with their versions) that ran.  Two assessments
are only comparable if their fingerprints match.  A score delta
between incomparable assessments may reflect grading changes rather
than real knowledge-base changes.

Usage:
    fp = compute_fingerprint(enabled_collectors=["topic_purity", ...])
    assessment.fingerprint = fp.to_dict()

    # Check comparability
    if not fingerprints_match(prev.fingerprint, curr.fingerprint):
        diff.comparable = False
        diff.comparability_warning = explain_mismatch(prev.fingerprint, curr.fingerprint)
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from ai_ready.invariants import POLICY_VERSION, SCHEMA_VERSION


@dataclass(frozen=True)
class ComparabilityFingerprint:
    """Immutable record of the measurement conditions for an assessment.

    Stored on KnowledgeAssessment so that diffs can detect when two
    assessments were produced under different grading conditions.

    Attributes:
        policy_version: Version of the interpretation policy (severity
            weights, score penalties).  Bumped in invariants.py.
        schema_version: Version of the data model (KnowledgeAssessment,
            KnowledgeSignal, DimensionScore structure).  Bumped in
            invariants.py.
        collector_ids: Sorted list of collector IDs that ran.  A diff
            between assessments with different collector sets is not
            meaningful because dimensions may be missing.
        collector_versions: Mapping of collector_id -> version string.
            Defaults to "1.0" for all collectors; individual collectors
            can override by setting a ``version`` class attribute.
        weights_hash: Hash of the dimension weight dict.  Ensures
            that changing weight allocation (even with the same
            policy version) is detected.
    """

    policy_version: str
    schema_version: str
    collector_ids: tuple[str, ...]
    collector_versions: dict[str, str] = field(default_factory=dict)
    weights_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "schema_version": self.schema_version,
            "collector_ids": list(self.collector_ids),
            "collector_versions": dict(self.collector_versions),
            "weights_hash": self.weights_hash,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> ComparabilityFingerprint | None:
        if not d:
            return None
        return cls(
            policy_version=d.get("policy_version", ""),
            schema_version=d.get("schema_version", ""),
            collector_ids=tuple(d.get("collector_ids", [])),
            collector_versions=dict(d.get("collector_versions", {})),
            weights_hash=d.get("weights_hash", ""),
        )

    @property
    def digest(self) -> str:
        """Short hash that uniquely identifies this fingerprint."""
        raw = json.dumps(self.to_dict(), sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def compute_fingerprint(
    enabled_collectors: list[str] | None = None,
    weights: dict[str, float] | None = None,
) -> ComparabilityFingerprint:
    """Build a fingerprint from the current measurement configuration.

    Args:
        enabled_collectors: List of collector IDs that ran.  If None,
            all registered collectors are assumed.
        weights: Dimension weight dict used for scoring.  If None,
            default weights from pipeline.py are used.
    """
    from ai_ready.rules import all_collectors

    # Determine which collectors ran
    registry = all_collectors()
    if enabled_collectors is not None:
        collector_ids = tuple(sorted(enabled_collectors))
    else:
        collector_ids = tuple(sorted(registry.keys()))

    # Collect per-collector versions (default "1.0")
    collector_versions: dict[str, str] = {}
    for cid in collector_ids:
        cls = registry.get(cid)
        if cls is not None and hasattr(cls, "version"):
            collector_versions[cid] = str(cls.version)
        else:
            collector_versions[cid] = "1.0"

    # Hash the weights dict so weight changes are detected even if
    # POLICY_VERSION wasn't bumped (defence in depth).
    if weights is None:
        from ai_ready.pipeline import DEFAULT_WEIGHTS
        weights = DEFAULT_WEIGHTS
    weights_hash = hashlib.sha256(
        json.dumps(weights, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]

    return ComparabilityFingerprint(
        policy_version=POLICY_VERSION,
        schema_version=SCHEMA_VERSION,
        collector_ids=collector_ids,
        collector_versions=collector_versions,
        weights_hash=weights_hash,
    )


def fingerprints_match(
    prev: dict[str, Any] | None,
    curr: dict[str, Any] | None,
) -> bool:
    """Check whether two fingerprint dicts are equivalent.

    Two assessments are comparable only if their fingerprints match.
    A None fingerprint (from an old assessment before fingerprints were
    added) is treated as incomparable with any non-None fingerprint
    and comparable with another None (both unknown → assume OK).
    """
    if prev is None and curr is None:
        return True
    if prev is None or curr is None:
        return False

    prev_fp = ComparabilityFingerprint.from_dict(prev)
    curr_fp = ComparabilityFingerprint.from_dict(curr)
    if prev_fp is None or curr_fp is None:
        return False

    return prev_fp.digest == curr_fp.digest


def explain_mismatch(
    prev: dict[str, Any] | None,
    curr: dict[str, Any] | None,
) -> str:
    """Produce a human-readable explanation of why fingerprints differ."""
    if prev is None and curr is None:
        return ""
    if prev is None:
        return "Previous assessment has no fingerprint (pre-fingerprint era). Comparability unknown."
    if curr is None:
        return "Current assessment has no fingerprint (pre-fingerprint era). Comparability unknown."

    prev_fp = ComparabilityFingerprint.from_dict(prev)
    curr_fp = ComparabilityFingerprint.from_dict(curr)
    if prev_fp is None or curr_fp is None:
        return "Fingerprint data is malformed."

    reasons: list[str] = []
    if prev_fp.policy_version != curr_fp.policy_version:
        reasons.append(
            f"Policy version changed: {prev_fp.policy_version} -> {curr_fp.policy_version}"
        )
    if prev_fp.schema_version != curr_fp.schema_version:
        reasons.append(
            f"Schema version changed: {prev_fp.schema_version} -> {curr_fp.schema_version}"
        )
    prev_set = set(prev_fp.collector_ids)
    curr_set = set(curr_fp.collector_ids)
    if prev_set != curr_set:
        added = curr_set - prev_set
        removed = prev_set - curr_set
        parts = []
        if added:
            parts.append(f"collectors added: {sorted(added)}")
        if removed:
            parts.append(f"collectors removed: {sorted(removed)}")
        reasons.append("Collector set changed: " + "; ".join(parts))
    if prev_fp.weights_hash != curr_fp.weights_hash:
        reasons.append("Dimension weights changed (hash mismatch)")
    for cid in prev_set & curr_set:
        pv = prev_fp.collector_versions.get(cid, "1.0")
        cv = curr_fp.collector_versions.get(cid, "1.0")
        if pv != cv:
            reasons.append(f"Collector '{cid}' version changed: {pv} -> {cv}")

    if not reasons:
        return ""

    return (
        "Assessments are not comparable — score delta may reflect "
        "measurement changes, not knowledge-base changes. Reasons: "
        + "; ".join(reasons) + "."
    )
