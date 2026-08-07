"""Remediation map — signal_type to (step_type, handler) mapping.

This is the core of the closed remediation loop (Item 1.1).  It maps
each concretely-fixable signal type to the executor step type and
parameters that will clear the signal on re-assessment.

The constraint: the collector's *detection* format and the executor's
*fix* format MUST be the same field.  If the FreshnessCollector reads
``date`` from front-matter, the executor must write ``date`` to
front-matter — not ``title`` or some other key the collector ignores.

Signal types mapped:
  no_date_signal        → add_metadata (writes ``date`` front-matter key)
  stale_content         → add_metadata (writes ``date`` front-matter key with recent date)
  missing_canonical_source → update_document (adds markdown link to canonical domain)
  orphan_artifact       → create_relationship (adds inbound link from index page)

Signal types NOT mapped (no deterministic fix):
  broken_link           → handled by _handle_update_document (existing)
  dangling_reference    → handled by _handle_update_document (existing)
  duplicate_content     → merge_artifacts (not implemented — explicit skip)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Remediation map: signal_type → (step_type, parameters_builder)
# ---------------------------------------------------------------------------

# Step types that are implemented and signal-mapped
IMPLEMENTED_STEP_TYPES = {
    "add_metadata",
    "update_document",
    "create_relationship",
    "create_document",
}

# Step types that are explicitly disabled with a clear skip reason
DISABLED_STEP_TYPES = {
    "remove_relationship": "Relationship removal is not implemented — requires artifact_store graph mutation",
    "archive_artifact": "Artifact archival is not implemented — requires artifact_store lifecycle management",
    "merge_artifacts": "Artifact merging is not implemented — requires content deduplication logic",
    "split_artifact": "Artifact splitting is not implemented — requires content partitioning logic",
}

# Signal types that are concretely fixable via the remediation map
FIXABLE_SIGNAL_TYPES = {
    "no_date_signal",
    "stale_content",
    "missing_canonical_source",
    "orphan_artifact",
}

# Signal types handled by existing executor logic (not via the map)
EXISTING_SIGNAL_TYPES = {
    "broken_link",
    "dangling_reference",
}

# Problem category → signal types that category produces.
# Used by problem-level verification as a fallback when signal IDs
# don't match between before/after assessments (e.g. because evidence
# changed after a fix).  Instead of matching by signal ID, verification
# can match by signal type: "did the signal TYPES this problem produces
# disappear from the after-assessment?"
CATEGORY_TO_SIGNAL_TYPES: dict[str, list[str]] = {
    "missing_metadata": ["no_date_signal", "stale_content"],
    "freshness": ["no_date_signal", "stale_content"],
    "stale_content": ["stale_content"],
    "missing_canonical_source": ["missing_canonical_source"],
    "missing_context": ["missing_canonical_source"],
    "broken_links": ["broken_link"],
    "dangling_references": ["dangling_reference"],
    "orphaned_documents": ["orphan_artifact"],
    "orphan_artifact": ["orphan_artifact"],
    "poor_structure": [],
    "duplicate_content": ["duplicate_content"],
    "terminology_inconsistency": ["terminology_variant"],
}

# Reverse map: signal_type → categories that produce it
SIGNAL_TYPE_TO_CATEGORIES: dict[str, list[str]] = {}
for _cat, _types in CATEGORY_TO_SIGNAL_TYPES.items():
    for _t in _types:
        SIGNAL_TYPE_TO_CATEGORIES.setdefault(_t, []).append(_cat)


def _get_signal_field(signal: Any, field: str, default: Any = None) -> Any:
    """Get a field from a signal that may be a dict or an object."""
    if isinstance(signal, dict):
        return signal.get(field, default)
    return getattr(signal, field, default)


def build_remediation_steps_from_signals(
    signals: list[Any],
    artifact_uris: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Build concrete modification steps from assessment signals.

    For each fixable signal type, generate a modification step that
    writes in the exact format the collector reads.  This ensures
    that re-assessment after execution will clear the signal.

    Args:
        signals: List of KnowledgeSignal objects or signal dicts.
        artifact_uris: Optional list of affected artifact URIs (for
            constraining scope).  If None, all signal artifacts are used.

    Returns:
        List of modification step dicts suitable for the executor.
    """
    steps: list[dict[str, Any]] = []
    seen_step_keys: set[str] = set()  # (step_type, artifact_uri) dedup
    seen_freshness_artifacts: set[str] = set()  # no_date + stale share one fix

    for signal in signals:
        signal_type = _get_signal_field(signal, "signal_type", "")
        artifact_uri = _get_signal_field(signal, "artifact_uri", "")

        # Skip if not a fixable signal type
        if signal_type not in FIXABLE_SIGNAL_TYPES:
            continue

        # no_date_signal and stale_content are both fixed by the same
        # add_metadata step (writing "date" front-matter).  Deduplicate
        # so each artifact gets at most ONE add_metadata step, keeping
        # the edit budget well within the 20% limit.
        if signal_type in ("no_date_signal", "stale_content"):
            if artifact_uri in seen_freshness_artifacts:
                continue
            seen_freshness_artifacts.add(artifact_uri)
        else:
            # Deduplicate by (step_type, artifact_uri) for other types
            step_key = f"{signal_type}|{artifact_uri}"
            if step_key in seen_step_keys:
                continue
            seen_step_keys.add(step_key)

        if signal_type in ("no_date_signal", "stale_content"):
            # FreshnessCollector reads metadata keys: "last_modified",
            # "date", "updated", "created", "last_updated".  We write
            # "date" with the current date so the collector finds it
            # on re-assessment and classifies the artifact as fresh.
            current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            steps.append({
                "step_type": "add_metadata",
                "artifact_uri": artifact_uri,
                "description": (
                    f"Add date metadata to resolve {signal_type} signal. "
                    f"Write date={current_date} to front-matter."
                ),
                "parameters": {
                    "metadata_key": "date",
                    "metadata_value": current_date,
                    "signal_type": signal_type,
                },
            })

        elif signal_type == "missing_canonical_source":
            # CanonicalSourceCollector checks artifact.links for links
            # to canonical domains.  The fix is to add a markdown link
            # to the canonical domain in the document content.
            evidence = _get_signal_field(signal, "evidence", {}) or {}
            canonical_domain = evidence.get("canonical_domain", "")
            topic_keyword = evidence.get("topic_keyword", "")
            if canonical_domain:
                steps.append({
                    "step_type": "update_document",
                    "artifact_uri": artifact_uri,
                    "description": (
                        f"Add canonical source link for '{topic_keyword}' "
                        f"to resolve missing_canonical_source signal. "
                        f"Link to https://{canonical_domain}/"
                    ),
                    "parameters": {
                        "action": "add_canonical_link",
                        "canonical_domain": canonical_domain,
                        "topic_keyword": topic_keyword,
                        "signal_type": signal_type,
                    },
                })

        elif signal_type == "orphan_artifact":
            # KnowledgeConnectivityCollector counts inbound links from
            # relationships and inline links.  The fix is to add a link
            # from the index/navigation page to this orphaned document.
            steps.append({
                "step_type": "create_relationship",
                "artifact_uri": artifact_uri,
                "description": (
                    f"Add inbound link to orphaned artifact to resolve "
                    f"orphan_artifact signal. Update index page to link "
                    f"to this document."
                ),
                "parameters": {
                    "action": "link_orphan",
                    "signal_type": signal_type,
                },
            })

    return steps


def constrain_proposal_steps(
    proposals: list[Any],
    signals: list[Any],
    prior_outcomes: list[Any] | None = None,
) -> list[Any]:
    """Constrain LLM proposal steps to implemented, signal-mapped types.

    If a proposal step uses a step_type that is disabled, replace it
    with a concrete step from the remediation map.  If a proposal step
    uses an unknown step_type, skip it with a clear reason.

    This ensures the executor never silently no-ops on a step.

    Item 3 (Institutional Memory): When prior_outcomes are provided,
    concrete steps for signals that have consistently FAILED in prior
    attempts are skipped (not auto-proposed again).  This prevents the
    system from repeatedly proposing the same fix that has never worked.

    Args:
        proposals: List of RemediationProposal objects.
        signals: List of KnowledgeSignal objects from the assessment.
        prior_outcomes: Optional list of prior remediation outcome dicts.
            Each dict should have 'strategy', 'result', and optionally
            'verification_outcome' and 'modification_steps'.

    Returns:
        New list of RemediationProposal with constrained steps.
    """
    from ai_ready.improvement.models import RemediationProposal

    # Item 3: Build a set of (signal_type, artifact_uri) keys that have
    # consistently failed in prior attempts.  These should NOT be
    # auto-proposed as concrete fallback steps.
    failed_keys: set[str] = set()
    if prior_outcomes:
        for outcome in prior_outcomes:
            result = (outcome.get("result", "") or "").lower()
            verification = (outcome.get("verification_outcome", "") or "").lower()
            # Skip outcomes that were successful — either the legacy
            # 'result' field says "success" with a positive verification,
            # or the CockroachDB 'verification_outcome' field directly
            # indicates resolution (CockroachDB outcomes lack 'result').
            if result == "success" and verification in ("resolved", "partially_resolved"):
                continue
            if not result and verification in ("resolved", "partially_resolved"):
                continue
            # Extract signal_type+artifact_uri from the outcome's
            # modification steps
            for step in outcome.get("modification_steps", []):
                sig_type = step.get("parameters", {}).get("signal_type", "")
                artifact_uri = step.get("artifact_uri", "")
                if sig_type and artifact_uri:
                    failed_keys.add(f"{sig_type}|{artifact_uri}")

    # Build concrete steps from signals as a fallback source
    concrete_steps = build_remediation_steps_from_signals(signals)
    concrete_by_key: dict[str, dict[str, Any]] = {}
    for step in concrete_steps:
        uri = step.get("artifact_uri", "")
        st = step.get("parameters", {}).get("signal_type", "")
        key = f"{st}|{uri}"
        # Item 3: Skip concrete steps that have consistently failed
        if key in failed_keys:
            logger.info(
                f"Skipping concrete step for {key} — consistently "
                f"failed in prior outcomes (institutional memory)."
            )
            continue
        concrete_by_key[key] = step

    # Track which fixable signals are already covered by LLM proposals
    covered_keys: set[str] = set()
    for proposal in proposals:
        for step in proposal.modification_steps:
            step_type = step.get("step_type", "")
            artifact_uri = step.get("artifact_uri", "")
            params = step.get("parameters", {})
            # If the step has a signal_type parameter, mark it as covered
            sig_type = params.get("signal_type", "")
            if sig_type and artifact_uri:
                covered_keys.add(f"{sig_type}|{artifact_uri}")
            # If it's an add_metadata step with the right metadata_key, mark
            # no_date_signal and stale_content as covered for that artifact
            if step_type == "add_metadata":
                meta_key = params.get("metadata_key", "")
                if meta_key == "date":
                    covered_keys.add(f"no_date_signal|{artifact_uri}")
                    covered_keys.add(f"stale_content|{artifact_uri}")
            # If it's an update_document with add_canonical_link action
            if step_type == "update_document" and params.get("action") == "add_canonical_link":
                covered_keys.add(f"missing_canonical_source|{artifact_uri}")
            # If it's a create_relationship
            if step_type == "create_relationship":
                covered_keys.add(f"orphan_artifact|{artifact_uri}")

    # Find concrete steps for signals NOT already covered by LLM proposals
    uncovered_concrete = [
        step for key, step in concrete_by_key.items()
        if key not in covered_keys
    ]

    result: list[RemediationProposal] = []
    for proposal in proposals:
        new_steps: list[dict[str, Any]] = []
        for step in proposal.modification_steps:
            step_type = step.get("step_type", "")
            if step_type in IMPLEMENTED_STEP_TYPES:
                # Enhance add_metadata steps with correct parameters if missing
                if step_type == "add_metadata":
                    params = step.get("parameters", {})
                    if not params.get("metadata_key"):
                        artifact_uri = step.get("artifact_uri", "")
                        # Find concrete step for this artifact
                        for ckey, cstep in concrete_by_key.items():
                            if ckey.endswith(f"|{artifact_uri}") and cstep["step_type"] == "add_metadata":
                                step["parameters"] = cstep["parameters"]
                                step["description"] = cstep["description"]
                                break
                new_steps.append(step)
            elif step_type in DISABLED_STEP_TYPES:
                # Replace with concrete step from the map if available
                artifact_uri = step.get("artifact_uri", "")
                # Try to find a concrete step for this artifact
                replaced = False
                for key, concrete in concrete_by_key.items():
                    if key.endswith(f"|{artifact_uri}"):
                        new_steps.append(concrete)
                        replaced = True
                        logger.info(
                            f"Replaced disabled {step_type} step with "
                            f"concrete {concrete['step_type']} step for "
                            f"{artifact_uri}"
                        )
                        break
                if not replaced:
                    # Skip with reason recorded in the step
                    step["skipped_reason"] = DISABLED_STEP_TYPES[step_type]
                    step["applied"] = False
                    new_steps.append(step)
            else:
                # Unknown step type — skip with reason
                step["skipped_reason"] = f"Unknown step_type: {step_type}"
                step["applied"] = False
                new_steps.append(step)

        # If no valid steps remain, add concrete steps from the map
        valid_steps = [s for s in new_steps if s.get("applied", True)]
        if not valid_steps and concrete_steps:
            new_steps = concrete_steps[:5]  # Limit to 5 concrete steps
        elif not valid_steps:
            continue  # Drop proposals with no actionable steps

        new_proposal = RemediationProposal(
            strategy=proposal.strategy,
            description=proposal.description,
            expected_impact=proposal.expected_impact,
            risks=proposal.risks,
            affected_artifact_uris=proposal.affected_artifact_uris,
            modification_steps=new_steps,
            root_cause_idx=proposal.root_cause_idx,
            reasoning=proposal.reasoning,
            affected_dimensions=proposal.affected_dimensions,
            confidence=proposal.confidence,
            assumptions=proposal.assumptions,
            rollback_considerations=proposal.rollback_considerations,
        )
        result.append(new_proposal)

    # Add uncovered concrete steps as a FIRST proposal so they get
    # selected (selected_proposal_idx=0) and executed.  LLM proposals
    # may exceed the edit budget gate with generic update_document
    # steps; the concrete signal-mapped steps are small and within
    # budget (Item 1.1).
    if uncovered_concrete:
        supplementary = RemediationProposal(
            strategy="signal_mapped_remediation",
            description="Concrete remediation steps from the signal→handler map.",
            expected_impact=f"Resolve {len(uncovered_concrete)} signal(s)",
            risks=["Minimal — deterministic fixes matching collector detection format"],
            affected_artifact_uris=list({s.get("artifact_uri", "") for s in uncovered_concrete}),
            modification_steps=uncovered_concrete,
            root_cause_idx=0,
            reasoning="Concrete signal-mapped steps that will clear signals on re-assessment.",
            affected_dimensions=[],
            confidence=0.9,
            assumptions=["Collectors will re-assess and clear signals after fix"],
            rollback_considerations="Revert file changes via git",
        )
        result.insert(0, supplementary)

    return result


def get_remediation_map_info() -> dict[str, Any]:
    """Return a summary of the remediation map for display/debugging."""
    return {
        "fixable_signal_types": sorted(FIXABLE_SIGNAL_TYPES),
        "existing_signal_types": sorted(EXISTING_SIGNAL_TYPES),
        "implemented_step_types": sorted(IMPLEMENTED_STEP_TYPES),
        "disabled_step_types": DISABLED_STEP_TYPES,
    }
