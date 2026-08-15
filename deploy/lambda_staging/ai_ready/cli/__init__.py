"""CLI package for AI-Ready.

Re-exports ``app`` from ``commands`` so that the entry point
``ai-ready = "ai_ready.cli:app"`` and ``python -m ai_ready.cli``
continue to work after the module was split into a package.
"""

from ai_ready.cli.commands import app

__all__ = ["app"]
