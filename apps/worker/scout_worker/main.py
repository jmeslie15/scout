"""Process entry point for the worker.

Same five responsibilities as the orchestrator's entry point, with one difference that matters: a
worker may be killed at any moment because it is disposable, so the shutdown path finishes the
**in-flight stage** rather than cancelling it. Cancelling would drop the results of work already
done inside a sandbox, and the reclaim sweep would then re-run the stage from scratch -- a test
suite executed twice for no reason.
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
from scout_persistence.queue import RedisJobQueue
from scout_persistence.session import create_engine_from_settings, create_session_factory
from scout_tools.artifacts import LocalArtifactStore
from scout_worker.settings import Settings, get_settings
from scout_worker.worker import Worker

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from scout_github import GitHubClient

__all__ = ["build_parser", "build_worker", "main", "run"]

_log = get_logger(__name__)

#: How long the process waits for the in-flight stage to finish after a stop signal, in seconds.
DEFAULT_SHUTDOWN_GRACE_SECONDS: Final = 120.0

#: Exit code returned when the grace period expired with a stage still running.
_EXIT_TIMEOUT = 75


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser, so ``--help`` is available without side effects."""
    parser = argparse.ArgumentParser(
        prog="python -m scout_worker",
        description=(
            "Scout worker: executes one verification pipeline stage per queue message inside an "
            "isolated sandbox, and records the result. It never decides job status."
        ),
        epilog=(
            "Configuration is read from the environment only (SCOUT_ prefix). A worker consumes "
            "the stage stream named by SCOUT_WORKER_STREAM and hands the job back to the "
            "orchestrator when the stage is done."
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="process the messages currently available and exit",
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


def build_worker(settings: Settings) -> tuple[Worker, Callable[[], Awaitable[None]]]:
    """Build the worker and its shutdown callback."""
    engine = create_engine_from_settings(settings.database_url)
    session_factory = create_session_factory(engine)
    artifacts = LocalArtifactStore(settings.artifact_root)
    github = _build_github(settings)
    queue = RedisJobQueue(
        settings.redis_url,
        stream=settings.worker_stream,
        group=settings.worker_consumer_group,
    )

    worker = Worker(
        settings=settings,
        session_factory=session_factory,
        github=github,
        queue=queue,
        artifacts=artifacts,
    )

    async def aclose() -> None:
        await queue.aclose()
        await engine.dispose()

    return worker, aclose


def _build_github(settings: Settings) -> GitHubClient:
    """Build the GitHub client, defaulting to the in-memory fake.

    The worker uses GitHub only to read the pull request's file list and description during
    PREPARING, but it must use the same client the orchestrator does or the two would disagree
    about what the change contains.
    """
    from scout_github import FakeGitHubClient, GitHubAppAuthenticator, HttpGitHubClient

    if not settings.github_app_id:
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
    import os

    raw = os.environ.get("SCOUT_GITHUB_INSTALLATION_ID", "").strip()
    if not raw.isdigit():
        raise ValueError(
            "SCOUT_GITHUB_INSTALLATION_ID must be set to a numeric installation id when a real "
            "GitHub App is configured"
        )
    return HttpGitHubClient(
        installation_id=int(raw),
        tokens=authenticator,
        base_url=settings.github_api_url,
    )


async def run(settings: Settings, *, once: bool = False, grace_seconds: float | None = None) -> int:
    """Run the worker until it stops. Returns the process exit code."""
    settings.validate_runtime_safety()
    grace = DEFAULT_SHUTDOWN_GRACE_SECONDS if grace_seconds is None else grace_seconds

    worker, aclose = build_worker(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):  # pragma: no cover - not available on Windows
            loop.add_signal_handler(signal_number, _request_stop(worker, stop, signal_number))

    try:
        if once:
            await worker.run_once(block_ms=0)
            return 0

        running = asyncio.create_task(worker.run_forever(stop=stop))
        stopped = asyncio.create_task(stop.wait())
        done, _pending = await asyncio.wait({running, stopped}, return_when=asyncio.FIRST_COMPLETED)
        if running in done:
            await running
            return 0

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


def _request_stop(worker: Worker, stop: asyncio.Event, signal_number: int) -> Callable[[], None]:
    """Build the signal handler that asks the loop to stop after the current stage."""

    def handler() -> None:
        _log.info("stop signal received", signal=signal_number, consumer=worker.consumer)
        worker.request_stop()
        stop.set()

    return handler


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the worker."""
    parser = build_parser()
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(
        level=settings.log_level, json_output=settings.log_json, service_name="scout-worker"
    )
    setup_tracing(service_name="scout-worker", enabled=settings.otel_enabled)

    if args.version:
        sys.stdout.write(f"scout-worker {settings.version}\n")
        return 0

    try:
        return asyncio.run(
            run(settings, once=bool(args.once), grace_seconds=float(args.shutdown_grace_seconds))
        )
    except KeyboardInterrupt:  # pragma: no cover - the signal handler normally wins the race
        return 130
