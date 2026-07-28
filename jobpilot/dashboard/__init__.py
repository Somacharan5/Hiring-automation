"""JobPilot Phase 5 — the monitoring dashboard (FastAPI + Jinja2)."""

from .app import create_app, dashboard_settings, run

__all__ = ["create_app", "run", "dashboard_settings"]
