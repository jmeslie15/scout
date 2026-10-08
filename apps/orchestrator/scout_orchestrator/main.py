"""Process entry point for the orchestrator.

Why this is its own module rather than a few lines in ``__main__``: a process entry point has to
do five things that are easy to get wrong and easy to test in isolation -- parse arguments, build
long-lived collaborators, install signal handling, run the loop, and tear everything down in a
bounded way. Keeping it here means ``__main__`` is a one-liner and the wiring is importable.

The shutdown contract matters most. On ``SIGTERM``/``SIGINT`` the loop is asked to stop (rather
than cancelled), so the **in-flight stage finishes** and its transaction commits. That is what
keeps a restart from leaving a ``RUNNING`` stage row behind for every deploy: a job abandoned
mid-transaction would be reclaimed by the lease sweep anyway, but it would be reclaimed having
done work nobody recorded, and the point of a bounded grace period is that it does not happen.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from contextlib import suppress
from typing import TYPE_CHECKING, Final

from scout_observability import (
    configure_logging,
    get_logger,
    setup_tracing,
    shutdown_tracing,
)
from scout_orchestrator.orchestrator import Orchestrator
from scout_orchestrator.settings import EXECUTION_MODE_WORKER, Settings, get_settings
from scout_persistence.queue import DEFAULT_CONSUMER_GROUP, DEFAULT_STREAM, RedisJobQueue
from scout_persistence.session import create_engine_from_settings, create_session_factory
from scout_tools.artifacts import LocalArtifactStore

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from scout_github import GitHubClient

__all__ = ["build_orchestrator", "build_parser", "main", "run"]

_log = get_logger(__name__)

#: How long the process waits for the in-flight stage to finish after a stop signal, in seconds.
#: Sized for a slow test suite inside a sandbox: long enough to be useful, short enough that a
#: deployment's own termination grace period is still honoured.
DEFAULT_SHUTDOWN_GRACE_SECONDS: Final = 120.0

#: Exit code returned when the shutdown grace period expired with work still running.
_EXIT_TIMEOUT = 75


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser, so ``--help`` is available without side effects."""
    parser = argparse.ArgumentParser(
        prog="python -m scout_orchestrator",
        description=(
            "Scout orchestrator: owns verification job state transitions and stage scheduling. "
            "Reserves queued jobs, reclaims expired leases, and drives the verification pipeline."
        ),
        epilog=(
            "Configuration is read from the environment only (SCOUT_ prefix). "
            "See docs/ARCHITECTURE.md section 4 for the stage pipeline and section 4.5 for "
            "retry and lease recovery."
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="process the messages currently available, sweep leases once, and exit",
    )
    parser.add_argument(
        "--shutdown-grace-seconds",
        type=float,
        default=DEFAULT_SHUTDOWN_GRACE_SECONDS,
        metavar="SECONDS",
        help=(
            "how long to wait for the in-flight stage to finish after SIGTERM/SIGINT "
            f"(default: {DEFAULT_SHUTDOWN_GRACE_SECONDS:.0f})"
        ),
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="print the version and exit",
    )
    return parser


def build_orchestrator(settings: Settings) -> tuple[Orchestrator, Callable[[], Awaitable[None]]]:
    """Build the orchestrator and its shutdown callback.

    Returns:
        ``(orchestrator, aclose)``. The callback disposes the engine and the queue, and is safe to
        call exactly once.
    """
    engine = create_engine_from_settings(settings.database_url)
    session_factory = create_session_factory(engine)
    artifacts = LocalArtifactStore(settings.artifact_root)
    github = _build_github(settings)
    queue = RedisJobQueue(settings.redis_url, stream=DEFAULT_STREAM, group=DEFAULT_CONSUMER_GROUP)
    stage_queue = (
        RedisJobQueue(
            settings.redis_url,
            stream=settings.worker_stream,
            group=settings.worker_consumer_group,
        )
        if settings.execution_mode == EXECUTION_MODE_WORKER
        else None
    )

    orchestrator = Orchestrator(
        settings=settings,
        session_factory=session_factory,
        artifacts=artifacts,
        github=github,
        queue=queue,
        stage_queue=stage_queue,
    )

    async def aclose() -> None:
        await queue.aclose()
        if stage_queue is not None:
            await stage_queue.aclose()
        await engine.dispose()

    return orchestrator, aclose


def _build_github(settings: Settings) -> GitHubClient:
    """Build the GitHub client, defaulting to the in-memory fake.

    The fake is the shipped default (``SCOUT_GITHUB_APP_ID`` blank), which is what makes the whole
    platform exercisable offline. A real App needs its private key, and an orchestrator without one
    must fail loudly rather than silently falling back to a fake that would record check runs
    nobody ever sees.
    """
    from scout_github import FakeGitHubClient, GitHubAppAuthenticator, HttpGitHubClient

    if not settings.github_app_configured:
        _log.warning(
            "no GitHub App is configured; using the in-memory fake client and recording check "
            "runs locally",
            github_app_id=settings.github_app_id,
        )
        return FakeGitHubClient()

    private_key = settings.github_private_key or None
    if private_key is None and settings.github_private_key_path is not None:
        private_key = settings.github_private_key_path.read_text(encoding="utf-8")
    if not private_key:
        raise ValueError(
            "a GitHub App is configured without a private key; set SCOUT_GITHUB_PRIVATE_KEY or "
            "SCOUT_GITHUB_PRIVATE_KEY_PATH"
        )

    authenticator = GitHubAppAuthenticator(
        app_id=settings.github_app_id,
        private_key_pem=private_key,
        api_base_url=settings.github_api_url,
    )
    return HttpGitHubClient(
        installation_id=_installation_id_from_environment(),
        tokens=authenticator,
        base_url=settings.github_api_url,
    )


def _installation_id_from_environment() -> int:
    """Read the installation id the orchestrator should authenticate as.

    Exposed as a function (rather than inline) so the error message can explain the deployment
    requirement: an orchestrator serves one installation in the initial single-tenant deployment
    described in ADR 0001.
    """
    import os

    raw = os.environ.get("SCOUT_GITHUB_INSTALLATION_ID", "").strip()
    if not raw.isdigit():
        raise ValueError(
            "SCOUT_GITHUB_INSTALLATION_ID must be set to a numeric installation id when a real "
            "GitHub App is configured"
        )
    return int(raw)


async def run(settings: Settings, *, once: bool = False, grace_seconds: float | None = None) -> int:
    """Run the orchestrator until it stops. Returns the process exit code."""
    settings.validate_runtime_safety()
    grace = DEFAULT_SHUTDOWN_GRACE_SECONDS if grace_seconds is None else grace_seconds

    orchestrator, aclose = build_orchestrator(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):  # pragma: no cover - not available on Windows
            loop.add_signal_handler(signal_number, _request_stop(orchestrator, stop, signal_number))

    try:
        if once:
            await orchestrator.reclaim_stale_messages()
            await orchestrator.sweep_expired_leases()
            await orchestrator.run_once(block_ms=0)
            return 0

        running = asyncio.create_task(orchestrator.run_forever(stop=stop))
        stopped = asyncio.create_task(stop.wait())
        done, _pending = await asyncio.wait({running, stopped}, return_when=asyncio.FIRST_COMPLETED)
        if running in done:
            # The loop exited on its own (a fatal error was raised rather than swallowed).
            await running
            return 0

        # A signal arrived: let the in-flight stage finish, within the grace period.
        _log.info("stop requested; waiting for the in-flight stage", grace_seconds=grace)
        try:
            await asyncio.wait_for(running, timeout=grace)
        except TimeoutError:
            _log.error(
                "the shutdown grace period expired with a stage still running; cancelling",
                grace_seconds=grace,
            )
            running.cancel()
            with suppress(asyncio.CancelledError):
                await running
            return _EXIT_TIMEOUT
        return 0
    finally:
        await aclose()
        shutdown_tracing()


def _request_stop(
    orchestrator: Orchestrator, stop: asyncio.Event, signal_number: int
) -> Callable[[], None]:
    """Build the signal handler that asks the loop to stop after the current stage."""

    def handler() -> None:
        _log.info("stop signal received", signal=signal_number, consumer=orchestrator.consumer)
        orchestrator.request_stop()
        stop.set()

    return handler


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the orchestrator."""
    parser = build_parser()
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        json_output=settings.log_json,
        service_name="scout-orchestrator",
    )
    setup_tracing(service_name="scout-orchestrator", enabled=settings.otel_enabled)

    if args.version:
        sys.stdout.write(f"scout-orchestrator {settings.version}\n")
        return 0

    try:
        return asyncio.run(
            run(settings, once=bool(args.once), grace_seconds=float(args.shutdown_grace_seconds))
        )
    except KeyboardInterrupt:  # pragma: no cover - the signal handler normally wins the race
        return 130
