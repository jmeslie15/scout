"""Materialising the pinned revision for a stage that needs files.

Every stage runs in its own workspace, and the workspace is disposable by design: a crashed worker
leaves a directory the next attempt reclaims by wiping it, and no stage can observe files another
stage's tool run left behind. The consequence is that a stage which needs the repository has to
materialise it, and this module is the one place that does so.

Why not keep one workspace for the whole pipeline? Because in ``worker`` execution mode each stage
is a separate message handled by a separate process, and state that exists only in memory violates
Rule 5. Threading a directory between processes would mean either a retained tree with stale
contents or a shared cache with its own eviction story -- both worse than a shallow fetch of two
commits, which is what :meth:`scout_sandbox.GitRunner.materialize` costs.

The fetch is pinned: ``git fetch <sha>`` by SHA, then a forced detach of that exact commit. A branch
name never appears, which is what spec section 13 requires.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from scout_sandbox import CheckoutResult, GitRunner, ensure_git_available, repo_url_for

if TYPE_CHECKING:
    from scout_orchestrator.stages.context import StageContext

__all__ = ["ensure_checkout"]


async def ensure_checkout(ctx: StageContext) -> CheckoutResult:
    """Materialise the job's pinned revision into this stage's workspace.

    Idempotent for a workspace that already holds the requested commit: a retained directory (an
    operator debugging a failed job) is re-described rather than re-fetched.

    Returns:
        The materialised range. The head SHA is read back from git, so a checkout that does not
        match the requested commit fails here instead of reporting on the wrong revision.
    """
    git = GitRunner(ctx.sandbox)
    await ensure_git_available(ctx.sandbox)
    repo_url = repo_url_for(ctx.job.repository_full_name, base_url=ctx.settings.git_base_url)

    if ctx.workspace.exists(".git"):
        actual = await git.rev_parse(ctx.workspace, "HEAD")
        if actual is not None and actual.startswith(ctx.job.head_sha[:12]):
            merge_base = await git.merge_base(ctx.workspace, ctx.job.base_sha, actual)
            changes = await git.diff_name_status(ctx.workspace, ctx.job.base_sha, actual)
            return CheckoutResult(
                head_sha=actual,
                base_sha=ctx.job.base_sha,
                merge_base_sha=merge_base,
                shallow=True,
                file_count=len(changes),
            )

    return await git.materialize(
        ctx.workspace,
        repo_url=repo_url,
        base_sha=ctx.job.base_sha,
        head_sha=ctx.job.head_sha,
        token=ctx.settings.git_token,
    )
