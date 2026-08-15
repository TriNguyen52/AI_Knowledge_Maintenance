"""Configuration parser for .ai-ready.yml."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ai_ready.pipeline import DEFAULT_WEIGHTS

class Config:
    """Parsed configuration from .ai-ready.yml."""

    def __init__(
        self,
        weights: dict[str, float] | None = None,
        thresholds: dict[str, Any] | None = None,
        fail_on: list[str] | None = None,
        enabled_collectors: dict[str, bool] | None = None,
        salience_threshold: float = 0.01,
        coefficient_thresholds: dict[str, int] | None = None,
        cap_thresholds: dict[str, int] | None = None,
    ) -> None:
        self.weights = weights or dict(DEFAULT_WEIGHTS)
        self.thresholds = thresholds or {"overall_score": 0}
        self.fail_on = fail_on or ["CRITICAL"]
        self.enabled_collectors = enabled_collectors or {}
        # P8: Salience threshold for problem discovery — problems below
        # this score are skipped (not actionable enough).  Default 0.01
        # matches the demo's hardcoded value.  Configurable via YAML:
        #   salience_threshold: 0.05
        self.salience_threshold = salience_threshold
        # Coefficient scoring thresholds — dimensions below these values
        # trigger multiplicative score reduction.  Configurable via YAML:
        #   coefficient_thresholds:
        #     connectivity: 30
        #     trust: 20
        #     freshness: 40
        self.coefficient_thresholds = coefficient_thresholds or None
        # Score cap thresholds — hard floors that prevent dimension masking.
        # Configurable via YAML:
        #   cap_thresholds:
        #     connectivity_critical: 30
        #     connectivity_cap: 50
        #     trust_critical: 20
        #     trust_cap: 40
        #     retrieval_critical: 30
        #     retrieval_cap: 45
        #     zero_dimension_cap: 30
        self.cap_thresholds = cap_thresholds or None

    @property
    def enabled_collector_ids(self) -> list[str]:
        """Return list of collector IDs that are enabled (or all if none specified)."""
        if not self.enabled_collectors:
            return []
        result = []
        for collector_id, val in self.enabled_collectors.items():
            if isinstance(val, dict):
                if val.get("enabled", True):
                    result.append(collector_id)
            elif val:
                result.append(collector_id)
        return result

    @classmethod
    def from_file(cls, path: str | Path) -> "Config":
        """Load config from a YAML file."""
        path = Path(path)
        if not path.exists():
            return cls()

        with open(path) as f:
            data = yaml.safe_load(f) or {}

        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        """Create config from a parsed dict."""
        enabled_collectors = data.get("collectors", {})
        return cls(
            weights=data.get("weights", dict(DEFAULT_WEIGHTS)),
            thresholds=data.get("thresholds", {"overall_score": 0}),
            fail_on=data.get("fail_on", ["CRITICAL"]),
            enabled_collectors=enabled_collectors,
            salience_threshold=data.get("salience_threshold", 0.01),
            coefficient_thresholds=data.get("coefficient_thresholds"),
            cap_thresholds=data.get("cap_thresholds"),
        )

    @classmethod
    def default(cls) -> "Config":
        """Create default config."""
        return cls()
