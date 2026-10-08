"""``FakeGitHubClient``: the offline implementation used when no GitHub App is configured.

The fake is what makes the platform exercisable end-to-end without GitHub, so its
behaviour is specified here: reads are strict, writes are recorded.
"""

from __future__ import annotations

import pytest

from scout_github.checks import CheckRunConclusion, CheckRunStatus
from scout_github.client import (
    DEFAULT_FAKE_BASE_SHA,
    DEFAULT_FAKE_HEAD_SHA,
    FakeGitHubClient,
    GitHubClient,
)
from scout_github.errors import GitHubNotFoundError
from scout_github.models import GitHubFile

BASE_SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
HEAD_SHA = "0f1e2d3c4b5a69788796a5b4c3d2e1f009182736"
FULL_NAME = "edrevel/scout-demo"


def _fake() -> FakeGitHubClient:
    fake = FakeGitHubClient()
    fake.register_repository(FULL_NAME)
    return fake


def _file(**overrides: object) -> GitHubFile:
    data: dict[str, object] = {
        "filename": "src/service.py",
        "status": "modified",
        "additions": 4,
        "deletions": 2,
        "patch": "@@ -1 +1 @@\n-old\n+new\n",
    }
    data.update(overrides)
    return GitHubFile.model_validate(data)


def test_fake_satisfies_the_protocol() -> None:
    assert isinstance(_fake(), GitHubClient)


# -- Reads ---------------------------------------------------------------------------


async def test_registered_repository_is_returned() -> None:
    fake = _fake()

    repository = await fake.get_repository(FULL_NAME)

    assert repository.full_name == FULL_NAME
    assert repository.default_branch == "main"


async def test_unregistered_repository_raises_not_found() -> None:
    fake = _fake()

    with pytest.raises(GitHubNotFoundError, match="register_repository"):
        await fake.get_repository("someone/else")


async def test_pull_request_registration_defaults_and_overrides() -> None:
    fake = _fake()
    fake.register_pull_request(
        FULL_NAME,
        42,
        title="Add verification",
        body=None,
        author="dev-author",
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
        draft=True,
    )

    pull_request = await fake.get_pull_request(FULL_NAME, 42)

    assert pull_request.title == "Add verification"
    assert pull_request.body is None
    assert pull_request.user is not None
    assert pull_request.user.login == "dev-author"
    assert pull_request.head.sha == HEAD_SHA
    assert pull_request.base.sha == BASE_SHA
    assert pull_request.draft is True


async def test_pull_request_registration_uses_predictable_default_shas() -> None:
    fake = _fake()
    fake.register_pull_request(FULL_NAME, 1)

    pull_request = await fake.get_pull_request(FULL_NAME, 1)

    assert pull_request.base.sha == DEFAULT_FAKE_BASE_SHA
    assert pull_request.head.sha == DEFAULT_FAKE_HEAD_SHA


async def test_registering_a_pull_request_auto_registers_its_repository() -> None:
    fake = FakeGitHubClient()
    fake.register_pull_request("owner/name", 1)

    assert (await fake.get_repository("owner/name")).full_name == "owner/name"


async def test_unregistered_pull_request_raises_not_found() -> None:
    fake = _fake()

    with pytest.raises(GitHubNotFoundError, match="register_pull_request"):
        await fake.get_pull_request(FULL_NAME, 999)


async def test_pull_request_files_can_be_registered_inline() -> None:
    fake = _fake()
    fake.register_pull_request(FULL_NAME, 42, files=[_file(), _file(filename="b.py")])

    files = await fake.list_pull_request_files(FULL_NAME, 42)

    assert [file.filename for file in files] == ["src/service.py", "b.py"]


async def test_pull_request_files_default_to_empty() -> None:
    fake = _fake()
    fake.register_pull_request(FULL_NAME, 42)

    assert await fake.list_pull_request_files(FULL_NAME, 42) == []


async def test_pull_request_files_for_an_unknown_pull_request_raise_not_found() -> None:
    fake = _fake()

    with pytest.raises(GitHubNotFoundError):
        await fake.list_pull_request_files(FULL_NAME, 7)


async def test_returned_file_list_is_a_copy() -> None:
    fake = _fake()
    fake.register_pull_request(FULL_NAME, 42, files=[_file()])

    files = await fake.list_pull_request_files(FULL_NAME, 42)
    files.clear()

    assert len(await fake.list_pull_request_files(FULL_NAME, 42)) == 1


async def test_commit_registration_and_lookup() -> None:
    fake = _fake()
    fake.register_commit(FULL_NAME, HEAD_SHA, message="Add the checker")

    commit = await fake.get_commit(FULL_NAME, HEAD_SHA)

    assert commit.message == "Add the checker"


async def test_unregistered_commit_raises_not_found() -> None:
    fake = _fake()

    with pytest.raises(GitHubNotFoundError, match="register_commit"):
        await fake.get_commit(FULL_NAME, HEAD_SHA)


async def test_registered_comparison_is_returned() -> None:
    fake = _fake()
    fake.register_compare(FULL_NAME, BASE_SHA, HEAD_SHA, files=[_file()])

    comparison = await fake.compare_commits(FULL_NAME, BASE_SHA, HEAD_SHA)

    assert comparison.files[0].filename == "src/service.py"


async def test_identical_commits_compare_without_registration() -> None:
    fake = _fake()

    comparison = await fake.compare_commits(FULL_NAME, HEAD_SHA, HEAD_SHA)

    assert comparison.status == "identical"
    assert comparison.files == []


async def test_unregistered_comparison_raises_not_found() -> None:
    fake = _fake()

    with pytest.raises(GitHubNotFoundError, match="register_compare"):
        await fake.compare_commits(FULL_NAME, BASE_SHA, HEAD_SHA)


async def test_issue_registration_marks_pull_requests() -> None:
    fake = _fake()
    fake.register_issue(FULL_NAME, 7, title="Requirement", body="Must do X")
    fake.register_issue(FULL_NAME, 8, title="A PR", is_pull_request=True)

    issue = await fake.get_issue(FULL_NAME, 7)

    assert issue.body == "Must do X"
    assert issue.pull_request is None
    assert (await fake.get_issue(FULL_NAME, 8)).pull_request is not None


async def test_unregistered_issue_raises_not_found() -> None:
    fake = _fake()

    with pytest.raises(GitHubNotFoundError, match="register_issue"):
        await fake.get_issue(FULL_NAME, 7)


async def test_file_contents_can_differ_per_ref() -> None:
    fake = _fake()
    fake.register_file_contents(FULL_NAME, "src/service.py", text="on main\n", ref="main")
    fake.register_file_contents(FULL_NAME, "src/service.py", text="on head\n", ref=HEAD_SHA)

    at_head = await fake.get_file_contents(FULL_NAME, "src/service.py", ref=HEAD_SHA)
    at_main = await fake.get_file_contents(FULL_NAME, "src/service.py", ref="main")

    assert at_head.decoded_text() == "on head\n"
    assert at_main.decoded_text() == "on main\n"


async def test_unregistered_file_contents_raise_not_found() -> None:
    fake = _fake()

    with pytest.raises(GitHubNotFoundError, match="register_file_contents"):
        await fake.get_file_contents(FULL_NAME, "missing.py")


# -- Writes --------------------------------------------------------------------------


async def test_check_runs_are_recorded_in_order() -> None:
    fake = _fake()

    first = await fake.create_check_run(
        FULL_NAME,
        HEAD_SHA,
        "Scout Verification",
        status=CheckRunStatus.IN_PROGRESS,
        title="Scout is verifying",
        summary="Running checks.",
        external_id="job_1",
    )
    second = await fake.create_check_run(
        FULL_NAME,
        HEAD_SHA,
        "Scout Verification",
        conclusion=CheckRunConclusion.SUCCESS,
        title="Scout verified this change",
        summary="All checks passed.",
        external_id="job_2",
    )

    assert [run.id for run in fake.check_runs] == [first.id, second.id]
    assert first.id == 1
    assert second.conclusion == "success"
    assert fake.check_runs[0].status == "in_progress"
    assert fake.check_runs[1].external_id == "job_2"
    assert fake.check_runs[0].output is not None
    assert fake.check_runs[0].output.summary == "Running checks."


async def test_check_run_payload_is_validated_like_the_real_api() -> None:
    fake = _fake()

    with pytest.raises(ValueError, match="requires a conclusion"):
        await fake.create_check_run(
            FULL_NAME,
            HEAD_SHA,
            "Scout Verification",
            status=CheckRunStatus.COMPLETED,
        )

    assert fake.check_runs == []


async def test_check_run_updates_are_recorded_separately_from_creations() -> None:
    fake = _fake()
    created = await fake.create_check_run(
        FULL_NAME,
        HEAD_SHA,
        "Scout Verification",
        status=CheckRunStatus.IN_PROGRESS,
        title="Scout is verifying",
        summary="Running checks.",
    )

    updated = await fake.update_check_run(
        FULL_NAME,
        created.id,
        conclusion=CheckRunConclusion.NEUTRAL,
        title="Human review required",
        summary="Needs a decision.",
    )

    assert len(fake.check_runs) == 1
    assert fake.check_runs[0].conclusion is None
    assert fake.check_run_updates == [updated]
    assert updated.conclusion == "neutral"
    assert updated.output is not None
    assert updated.output.summary == "Needs a decision."


async def test_output_only_update_leaves_status_and_conclusion_untouched() -> None:
    fake = _fake()
    created = await fake.create_check_run(
        FULL_NAME,
        HEAD_SHA,
        "Scout Verification",
        status=CheckRunStatus.IN_PROGRESS,
        title="Scout is verifying",
        summary="Running checks.",
    )

    updated = await fake.update_check_run(FULL_NAME, created.id, summary="Still running.")

    assert updated.status == "in_progress"
    assert updated.conclusion is None
    assert updated.output is not None
    assert updated.output.summary == "Still running."
    assert updated.output.title == "Scout is verifying"


async def test_updating_an_unknown_check_run_raises_not_found() -> None:
    fake = _fake()

    with pytest.raises(GitHubNotFoundError, match="no fake check run"):
        await fake.update_check_run(FULL_NAME, 999, conclusion=CheckRunConclusion.SUCCESS)


async def test_comments_are_recorded_with_their_request_context() -> None:
    fake = _fake()

    comment = await fake.create_pull_request_comment(
        FULL_NAME, 42, "Which retry policy applies here?"
    )

    assert len(fake.comments) == 1
    record = fake.comments[0]
    assert record.full_name == FULL_NAME
    assert record.number == 42
    assert record.body == "Which retry policy applies here?"
    assert record.comment.id == comment.id
    assert record.id == comment.id  # the ledger can stand in for the comment itself
    assert comment.user is not None
    assert comment.user.type == "Bot"


async def test_check_runs_and_comments_do_not_require_registration() -> None:
    """A flow that reports back is testable on its own, without a registered PR."""
    fake = FakeGitHubClient()

    check_run = await fake.create_check_run(
        FULL_NAME,
        HEAD_SHA,
        "Scout Verification",
        conclusion=CheckRunConclusion.SUCCESS,
    )
    await fake.create_pull_request_comment(FULL_NAME, 42, "Report available.")

    assert check_run.id == 1
    assert len(fake.comments) == 1


async def test_comment_ids_are_sequential() -> None:
    fake = _fake()

    first = await fake.create_pull_request_comment(FULL_NAME, 42, "one")
    second = await fake.create_pull_request_comment(FULL_NAME, 42, "two")

    assert (first.id, second.id) == (1, 2)


async def test_unregistered_repository_names_report_not_found() -> None:
    fake = _fake()

    # A naive dict lookup would raise KeyError and escape the typed error contract.
    with pytest.raises(GitHubNotFoundError):
        await fake.get_repository("")
    with pytest.raises(GitHubNotFoundError):
        await fake.get_repository("no-slash")
