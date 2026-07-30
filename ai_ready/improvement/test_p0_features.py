"""Tests for P0 features: recency decay and wont-fix suppression."""

import json
import sqlite3
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from ai_ready.improvement.salience import (
    compute_staleness_factor,
    compute_problem_salience,
    rank_problems_by_salience,
    FAST_HALF_LIFE_DAYS,
    SLOW_HALF_LIFE_DAYS,
    SLOW_FLOOR_WEIGHT,
    DECAY_EPSILON,
)
from ai_ready.improvement.models import KnowledgeProblem
from ai_ready.improvement.problem_queue import ProblemQueue


# ---------------------------------------------------------------------------
# Staleness factor tests
# ---------------------------------------------------------------------------

def test_staleness_factor_no_lifecycle():
    """No lifecycle data → no decay (backwards compatible)."""
    problem = KnowledgeProblem(
        problem_id="p1",
        category="missing_doc",
        description="test",
        root_cause_idx=0,
        artifact_uris=["a.md"],
        signal_ids=["s1"],
    )
    factor = compute_staleness_factor(problem, signal_lifecycles=None)
    assert factor == 1.0


def test_staleness_factor_empty_lifecycle():
    """Empty lifecycle dict → no decay."""
    problem = KnowledgeProblem(
        problem_id="p1",
        category="missing_doc",
        description="test",
        root_cause_idx=0,
        artifact_uris=["a.md"],
        signal_ids=["s1"],
    )
    factor = compute_staleness_factor(problem, signal_lifecycles={})
    assert factor == 1.0


def test_staleness_factor_never_seen():
    """Signal never seen → epsilon (minimal weight, still ordinal)."""
    problem = KnowledgeProblem(
        problem_id="p1",
        category="missing_doc",
        description="test",
        root_cause_idx=0,
        artifact_uris=["a.md"],
        signal_ids=["s1"],
    )
    # Lifecycle exists but has no last_seen for this signal
    factor = compute_staleness_factor(problem, signal_lifecycles={"s2": {"last_seen": "2026-01-01T00:00:00+00:00"}})
    assert factor == DECAY_EPSILON


def test_staleness_factor_recent():
    """Recently seen → close to 1.0."""
    now = datetime.now(timezone.utc)
    problem = KnowledgeProblem(
        problem_id="p1",
        category="missing_doc",
        description="test",
        root_cause_idx=0,
        artifact_uris=["a.md"],
        signal_ids=["s1"],
    )
    # 1 day old
    one_day_ago = (now - timedelta(days=1)).isoformat()
    factor = compute_staleness_factor(problem, signal_lifecycles={"s1": {"last_seen": one_day_ago}}, now=now)
    # Should be close to 1 (fast term ~0.967 + slow floor ~0.099)
    assert factor > 0.9
    assert factor < 1.2  # bounded


def test_staleness_factor_old():
    """Old signal → significantly decayed."""
    now = datetime.now(timezone.utc)
    problem = KnowledgeProblem(
        problem_id="p1",
        category="missing_doc",
        description="test",
        root_cause_idx=0,
        artifact_uris=["a.md"],
        signal_ids=["s1"],
    )
    # 60 days old
    old = (now - timedelta(days=60)).isoformat()
    factor = compute_staleness_factor(problem, signal_lifecycles={"s1": {"last_seen": old}}, now=now)
    # Fast term: 0.5^(60/21) ≈ 0.13
    # Slow floor: 0.1 * 0.5^(60/180) ≈ 0.079
    # Total ~0.21
    assert factor < 0.3
    assert factor > DECAY_EPSILON


def test_staleness_factor_very_old():
    """Very old signal → near epsilon."""
    now = datetime.now(timezone.utc)
    problem = KnowledgeProblem(
        problem_id="p1",
        category="missing_doc",
        description="test",
        root_cause_idx=0,
        artifact_uris=["a.md"],
        signal_ids=["s1"],
    )
    # 365 days old
    very_old = (now - timedelta(days=365)).isoformat()
    factor = compute_staleness_factor(problem, signal_lifecycles={"s1": {"last_seen": very_old}}, now=now)
    # Should be very close to epsilon
    assert factor < 0.05
    assert factor >= DECAY_EPSILON


def test_staleness_factor_multiple_signals():
    """Uses most recent last_seen across all signals."""
    now = datetime.now(timezone.utc)
    problem = KnowledgeProblem(
        problem_id="p1",
        category="missing_doc",
        description="test",
        root_cause_idx=0,
        artifact_uris=["a.md"],
        signal_ids=["s1", "s2", "s3"],
    )
    # s1: 30 days old, s2: 5 days old, s3: 100 days old
    lifecycles = {
        "s1": {"last_seen": (now - timedelta(days=30)).isoformat()},
        "s2": {"last_seen": (now - timedelta(days=5)).isoformat()},
        "s3": {"last_seen": (now - timedelta(days=100)).isoformat()},
    }
    factor = compute_staleness_factor(problem, signal_lifecycles=lifecycles, now=now)
    # Should use s2 (5 days old) — most recent
    # 5 days: fast ~0.85, slow floor ~0.098, total ~0.95
    assert factor > 0.85


def test_staleness_factor_dual_half_life_formula():
    """Verify the dual half-life formula math."""
    now = datetime.now(timezone.utc)
    age_days = 42.0
    problem = KnowledgeProblem(
        problem_id="p1",
        category="missing_doc",
        description="test",
        root_cause_idx=0,
        artifact_uris=["a.md"],
        signal_ids=["s1"],
    )
    old = (now - timedelta(days=age_days)).isoformat()
    factor = compute_staleness_factor(problem, signal_lifecycles={"s1": {"last_seen": old}}, now=now)
    
    # Manual computation
    fast_term = 0.5 ** (age_days / FAST_HALF_LIFE_DAYS)
    slow_floor = SLOW_FLOOR_WEIGHT * 0.5 ** (age_days / SLOW_HALF_LIFE_DAYS)
    expected = max(DECAY_EPSILON, fast_term + slow_floor)
    
    assert abs(factor - expected) < 0.001


# ---------------------------------------------------------------------------
# Wont-fix suppression tests
# ---------------------------------------------------------------------------

class MockAssessment:
    """Minimal mock for assessment."""
    def __init__(self, assessment_id, signals):
        self.assessment_id = assessment_id
        self.signals = signals


class MockSignal:
    """Minimal mock for signal."""
    def __init__(self, signal_id, severity, artifact_uri):
        self.signal_id = signal_id
        self.severity = severity
        self.artifact_uri = artifact_uri


def test_wont_fix_signature_computation():
    """Signature is category + sorted artifact URIs."""
    queue = ProblemQueue()
    
    p1 = KnowledgeProblem(
        problem_id="p1",
        category="missing_doc",
        description="test",
        root_cause_idx=0,
        artifact_uris=["b.md", "a.md"],  # unsorted
        signal_ids=["s1"],
    )
    sig1 = queue._problem_signature(p1)
    
    p2 = KnowledgeProblem(
        problem_id="p2",
        category="missing_doc",
        description="different problem",
        root_cause_idx=0,
        artifact_uris=["a.md", "b.md"],  # different order
        signal_ids=["s2"],
    )
    sig2 = queue._problem_signature(p2)
    
    # Same signature despite different order
    assert sig1 == sig2
    assert sig1 == "missing_doc|a.md|b.md"


def test_wont_fix_different_category():
    """Different category → different signature."""
    queue = ProblemQueue()
    
    p1 = KnowledgeProblem(
        problem_id="p1",
        category="missing_doc",
        description="test",
        root_cause_idx=0,
        artifact_uris=["a.md"],
        signal_ids=["s1"],
    )
    
    p2 = KnowledgeProblem(
        problem_id="p2",
        category="stale_doc",  # different category
        description="test",
        root_cause_idx=0,
        artifact_uris=["a.md"],
        signal_ids=["s2"],
    )
    
    assert queue._problem_signature(p1) != queue._problem_signature(p2)


def test_wont_fix_suppression():
    """Problems matching wont_fix signature are filtered before discovery."""
    queue = ProblemQueue()
    
    # Create a problem and mark it wont_fix
    p1 = KnowledgeProblem(
        problem_id="p1",
        category="missing_doc",
        description="test",
        root_cause_idx=0,
        artifact_uris=["a.md"],
        signal_ids=["s1"],
    )
    
    # Manually insert into queue and register signature
    queue._add_wont_fix_signature(p1)
    queue._conn.commit()
    
    # Check suppression
    assert queue._is_suppressed(p1)
    
    # Different problem with same signature should also be suppressed
    p2 = KnowledgeProblem(
        problem_id="p2",
        category="missing_doc",
        description="different problem",
        root_cause_idx=0,
        artifact_uris=["a.md"],
        signal_ids=["s2"],
    )
    assert queue._is_suppressed(p2)


def test_wont_fix_clear_signature():
    """Clearing signature allows re-discovery."""
    queue = ProblemQueue()
    
    p1 = KnowledgeProblem(
        problem_id="p1",
        category="missing_doc",
        description="test",
        root_cause_idx=0,
        artifact_uris=["a.md"],
        signal_ids=["s1"],
    )
    
    queue._add_wont_fix_signature(p1)
    queue._conn.commit()
    
    sig = queue._problem_signature(p1)
    assert queue._is_suppressed(p1)
    
    # Clear the signature
    assert queue.clear_wont_fix_signature(sig) is True
    assert not queue._is_suppressed(p1)
    
    # Clearing non-existent signature returns False
    assert queue.clear_wont_fix_signature("nonexistent|sig") is False


def test_wont_fix_persistence():
    """Suppression signatures persist across instances."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_queue.db"
        
        # First instance: add signature
        queue1 = ProblemQueue(db_path=db_path)
        p1 = KnowledgeProblem(
            problem_id="p1",
            category="missing_doc",
            description="test",
            root_cause_idx=0,
            artifact_uris=["a.md"],
            signal_ids=["s1"],
        )
        queue1._add_wont_fix_signature(p1)
        queue1._conn.commit()
        queue1._conn.close()  # Close connection before cleanup
        
        # Second instance: check it's still there
        queue2 = ProblemQueue(db_path=db_path)
        assert queue2._is_suppressed(p1)
        queue2._conn.close()  # Close connection before cleanup


# ---------------------------------------------------------------------------
# Integration: salience with staleness
# ---------------------------------------------------------------------------

def test_salience_includes_staleness_factor():
    """ProblemSalience includes staleness_factor in decomposition."""
    from ai_ready.models import Severity, KnowledgeSignal
    
    now = datetime.now(timezone.utc)
    
    # Create a signal
    signal = KnowledgeSignal(
        signal_id="s1",
        collector_id="test",
        signal_type="missing",
        severity=Severity.HIGH,
        score=70,
        artifact_id="a.md",
        artifact_uri="a.md",
        evidence={},
        recommendation="fix it",
        ai_impact="blocks retrieval",
    )
    
    # Create a problem
    problem = KnowledgeProblem(
        problem_id="p1",
        category="missing_doc",
        description="test",
        root_cause_idx=0,
        artifact_uris=["a.md"],
        signal_ids=["s1"],
    )
    
    # Mock assessment
    class MockAssess:
        assessment_id = "a1"
        signals = [signal]
        dimensions = {}
    
    # Old lifecycle
    old = (now - timedelta(days=90)).isoformat()
    lifecycles = {"s1": {"last_seen": old}}
    
    salience = compute_problem_salience(problem, MockAssess(), None, lifecycles)
    
    assert salience.staleness_factor < 1.0
    assert salience.staleness_factor >= DECAY_EPSILON
    
    # Total should be reduced by staleness
    # Without staleness, encoding would be ~0.7 (HIGH severity)
    # With 90-day staleness, factor ~0.07, so total ~0.05
    assert salience.total < 0.15  # significantly decayed


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
