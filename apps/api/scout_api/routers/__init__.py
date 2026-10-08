"""HTTP routers, grouped by the resource they expose."""

from __future__ import annotations

from scout_api.routers import catalog, health, jobs, questions, webhooks

__all__ = ["catalog", "health", "jobs", "questions", "webhooks"]
