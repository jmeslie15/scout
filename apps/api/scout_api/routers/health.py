"""Health and readiness endpoints (spec section 61).

``/health`` and ``/ready`` answer different questions and must not be conflated:

* ``/health`` — "is this process alive?" Used by the container runtime to decide whether to
  restart. It must never touch a dependency, because a database blip should not cause a
  restart loop.
* ``/ready`` — "can this process serve traffic?" Used by a load balancer and by the
  operator. It probes every dependency and reports each one individually, so a partial
  outage is diagnosable from a single ``curl`` rather than from the logs.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Request
from redis.asyncio import Redis

from scout_api.deps import app_state, settings_dependency
from scout_api.schemas import DependencyCheck, HealthResponse, ReadyResponse
from scout_api.settings import Settings
from scout_domain.models.artifact import ArtifactKind
from scout_persistence.session import check_database

__all__ = ["router"]

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
async def health(settings: Annotated[Settings, settings_dependency]) -> HealthResponse:
    """Report that the process is running.

    Deliberately dependency-free: this endpoint answering 200 means "do not restart me",
    not "everything works".
    """
    return HealthResponse(status="ok", service="scout-api", version=settings.version)


@router.get("/ready", response_model=ReadyResponse, summary="Readiness probe")
async def ready(
    request: Request,
    settings: Annotated[Settings, settings_dependency],
) -> ReadyResponse:
    """Probe every dependency and report each one separately.

    Returns 200 with ``status="degraded"`` rather than a 5xx when a non-critical dependency
    is down. The API can still serve job history while Redis is unavailable, and reporting
    that honestly is more useful than a blanket failure. The orchestrator's own readiness is
    what gates job processing.
    """
    state = app_state(request)
    checks: dict[str, DependencyCheck] = {}

    # Database: required. Without it the API cannot serve anything meaningful.
    try:
        await check_database(state.engine)
        checks["database"] = DependencyCheck(ok=True)
    except Exception as exc:
        checks["database"] = DependencyCheck(ok=False, detail=f"{type(exc).__name__}: {exc}")

    # Redis: required for job intake. A job can be recorded but not queued without it.
    try:
        client = Redis.from_url(settings.redis_url, decode_responses=True)
        try:
            await client.ping()
            checks["redis"] = DependencyCheck(ok=True)
        finally:
            close = getattr(client, "aclose", None)
            if close is not None:
                await close()
    except Exception as exc:
        checks["redis"] = DependencyCheck(ok=False, detail=f"{type(exc).__name__}: {exc}")

    # Artifact store: required to preserve evidence, so its absence is a real degradation.
    try:
        probe = state.artifacts.put_text(
            "scout-readiness-probe", kind=ArtifactKind.LOG, filename="readiness.txt"
        )
        ok = state.artifacts.exists(probe)
        checks["artifacts"] = DependencyCheck(
            ok=ok, detail=None if ok else "write succeeded but the artifact could not be read back"
        )
    except Exception as exc:
        checks["artifacts"] = DependencyCheck(ok=False, detail=f"{type(exc).__name__}: {exc}")

    # LLM provider: informational. Scout runs without a model provider (the fake provider is
    # the shipped default), so this must never make the API unready.
    checks["llm"] = DependencyCheck(
        ok=True,
        detail="fake provider" if not state.settings.github_app_configured else "configured",
    )

    overall = (
        "ready"
        if all(
            check.ok for name, check in checks.items() if name in {"database", "redis", "artifacts"}
        )
        else "degraded"
    )
    return ReadyResponse(status=overall, checks=checks)
