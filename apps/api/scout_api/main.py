"""FastAPI application factory and process entry point.

The application is assembled here and nowhere else, so the wiring of the process — which
dependencies exist, how they are disposed, which routers are mounted, how errors are shaped —
is reviewable in one file.

Design notes:

* The lifespan hook owns every long-lived resource and disposes it on shutdown. Nothing is
  created at import time, so importing this module has no side effects and a test can build an
  app with its own dependencies via ``app.dependency_overrides``.
* Errors are shaped into one body (``code`` + ``message`` + ``detail``) so the dashboard has a
  single error-rendering path rather than one per endpoint.
* CORS is restricted to the configured dashboard origin. An internal tool still should not
  answer ``Access-Control-Allow-Origin: *``.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from scout_api.deps import AppState
from scout_api.routers import catalog, health, jobs, questions, webhooks
from scout_api.settings import Settings, get_settings
from scout_domain.errors import ScoutDomainError
from scout_observability import (
    bind_context,
    clear_context,
    configure_logging,
    get_logger,
    setup_tracing,
    shutdown_tracing,
)
from scout_persistence.queue import JobQueue, RedisJobQueue
from scout_persistence.session import (
    create_engine_from_settings,
    create_session_factory,
    dispose_engine,
)
from scout_tools.artifacts import LocalArtifactStore

__all__ = ["app", "create_app"]

_log = get_logger(__name__)


def _build_github_client(settings: Settings) -> object:
    """Construct the GitHub client for this process.

    When no GitHub App is configured the in-memory fake is used, which is the shipped default
    so that the whole platform is exercisable offline (spec section 9 — the interface is the
    contract, not the implementation).

    Multi-installation routing is deliberately not implemented here: ``HttpGitHubClient`` is
    bound to one installation, so a deployment serving several would cache one client per
    installation id. A single-installation deployment is what the initial target needs, and
    building the multi-tenant layer before it is required would violate Rule 1.
    """
    from scout_github import FakeGitHubClient, GitHubAppAuthenticator, HttpGitHubClient

    if not settings.github_app_configured:
        _log.warning(
            "no GitHub App is configured; using the in-memory fake client. Webhooks still "
            "create jobs, but no check run reaches GitHub.",
        )
        return FakeGitHubClient()

    installation_id = os.environ.get("SCOUT_GITHUB_INSTALLATION_ID", "").strip()
    if not installation_id.isdigit():
        _log.warning(
            "a GitHub App is configured but SCOUT_GITHUB_INSTALLATION_ID is missing; "
            "falling back to the fake client",
        )
        return FakeGitHubClient()

    # `from_environment` reads SCOUT_GITHUB_APP_ID, SCOUT_GITHUB_PRIVATE_KEY_PATH and
    # SCOUT_GITHUB_PRIVATE_KEY, so the private key never has to be threaded through an
    # application object (spec section 55: secrets come from configuration management).
    authenticator = GitHubAppAuthenticator.from_environment()
    return HttpGitHubClient(
        installation_id=int(installation_id),
        tokens=authenticator,
        base_url=settings.github_api_url,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own every long-lived resource for the process lifetime."""
    settings = get_settings()
    settings.validate_runtime_safety()

    configure_logging(
        level=settings.log_level, json_output=settings.log_json, service_name="scout-api"
    )
    setup_tracing(service_name="scout-api")

    engine = create_engine_from_settings(settings.database_url)
    session_factory = create_session_factory(engine)
    artifacts = LocalArtifactStore(settings.artifact_root)
    queue: JobQueue = RedisJobQueue(settings.redis_url)

    app.state.scout = AppState(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        artifacts=artifacts,
        github=_build_github_client(settings),  # type: ignore[arg-type]
        queue=queue,
    )

    _log.info(
        "scout api started",
        env=settings.env,
        version=settings.version,
        database=settings.database_url.rsplit("@", 1)[-1],
        artifact_root=str(settings.artifact_root),
        github_app=settings.github_app_configured,
    )
    try:
        yield
    finally:
        # Disposal order is deliberate: drain the queue client, then the connection pool.
        try:
            await queue.aclose()
        except Exception as exc:
            _log.warning("failed to close the job queue", error=str(exc))
        await dispose_engine(engine)
        shutdown_tracing()
        _log.info("scout api stopped")


def create_app() -> FastAPI:
    """Build the API application."""
    settings = get_settings()

    app = FastAPI(
        title="Scout API",
        description=(
            "Edrevel AI Scout — engineering verification control plane. "
            "AI investigates and recommends. Deterministic systems verify. Humans authorize."
        ),
        version=settings.version,
        lifespan=lifespan,
        # The dashboard is the only consumer; an interactive docs page is a development
        # convenience that should not be exposed by default in production.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.dashboard_url],
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["content-type", "accept"],
    )

    @app.middleware("http")
    async def correlation_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Attach a correlation id to every request and log its outcome.

        The id is taken from the caller when supplied (``X-Request-Id``, and for webhooks the
        GitHub delivery id) so a single identifier threads through the webhook, the queue, the
        orchestrator, and the report.
        """
        clear_context()
        request_id = (
            request.headers.get("x-request-id")
            or request.headers.get("x-github-delivery")
            or f"req_{uuid.uuid4().hex[:16]}"
        )
        bind_context(
            correlation_id=request_id,
            http_method=request.method,
            http_path=request.url.path,
        )
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            _log.exception("request failed with an unhandled exception")
            raise
        finally:
            clear_context()
        duration_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Request-Id"] = request_id
        if request.url.path not in {"/health", "/ready"}:
            _log.info(
                "http request",
                status_code=response.status_code,
                duration_ms=round(duration_ms, 1),
                request_id=request_id,
            )
        return response

    # -- Error shaping -------------------------------------------------------------
    @app.exception_handler(ScoutDomainError)
    async def domain_error_handler(_request: Request, exc: ScoutDomainError) -> JSONResponse:
        """Map a domain error to a 409 with a stable code.

        A domain error is a *refusal* of an invalid operation, not a server fault, so it must
        not surface as a 500.
        """
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "code": type(exc).__name__,
                "message": exc.message,
                "detail": {key: str(value) for key, value in exc.context.items()},
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(_request: Request, exc: Exception) -> JSONResponse:
        """Return a shaped 500 and log the traceback.

        The response deliberately does not echo the exception: an internal error's text can
        contain a connection string or a file path, and this endpoint is reachable by anything
        that can reach the API.
        """
        _log.exception("unhandled server error", error_type=type(exc).__name__)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "code": "internal_error",
                "message": "an internal error occurred; the details are in the server log",
                "detail": {"error_type": type(exc).__name__},
            },
        )

    # -- Routes ---------------------------------------------------------------------
    app.include_router(health.router)
    app.include_router(webhooks.router)
    app.include_router(jobs.router)
    app.include_router(questions.router)
    app.include_router(catalog.router)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, object]:
        """Advertise the service and the endpoints that matter."""
        return {
            "service": "scout-api",
            "version": settings.version,
            "principle": (
                "AI investigates and recommends. Deterministic systems verify. Humans authorize."
            ),
            "endpoints": {
                "health": "/health",
                "ready": "/ready",
                "webhook": "/api/v1/webhooks/github",
                "jobs": "/api/v1/jobs",
                "questions": "/api/v1/questions",
                "repositories": "/api/v1/repositories",
                "tools": "/api/v1/tools",
                "agents": "/api/v1/agents",
                "metrics": "/api/v1/metrics/summary",
            },
        }

    return app


app = create_app()
