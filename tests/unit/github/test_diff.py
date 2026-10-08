"""GitHub file entries to Scout change sets and unified diffs (spec section 16)."""

from __future__ import annotations

import pytest

from scout_domain.models.repository import ChangeType
from scout_github.diff import (
    GITHUB_STATUS_TO_CHANGE_TYPE,
    build_change_set,
    change_type_for_status,
    is_binary_file,
    render_unified_diff,
    to_changed_file,
)
from scout_github.errors import GitHubError
from scout_github.models import GitHubFile

BASE_SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
HEAD_SHA = "0f1e2d3c4b5a69788796a5b4c3d2e1f009182736"


def _file(**overrides: object) -> GitHubFile:
    data: dict[str, object] = {
        "filename": "src/service.py",
        "status": "modified",
        "additions": 4,
        "deletions": 2,
        "changes": 6,
        "patch": "@@ -1,3 +1,4 @@\n context\n-removed\n+added\n+another\n",
    }
    data.update(overrides)
    return GitHubFile.model_validate(data)


# -- Status mapping ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("added", ChangeType.ADDED),
        ("removed", ChangeType.DELETED),
        ("modified", ChangeType.MODIFIED),
        ("renamed", ChangeType.RENAMED),
        ("copied", ChangeType.COPIED),
        ("changed", ChangeType.MODIFIED),
        ("unchanged", ChangeType.MODIFIED),
        ("ADDED", ChangeType.ADDED),
        (" Removed ", ChangeType.DELETED),
    ],
)
def test_status_mapping(status: str, expected: ChangeType) -> None:
    assert change_type_for_status(status) is expected


def test_removed_maps_to_deleted_not_modified() -> None:
    """GitHub says ``removed``; Scout's vocabulary says ``DELETED``."""
    assert change_type_for_status("removed") is ChangeType.DELETED
    assert change_type_for_status("removed") is not ChangeType.MODIFIED


def test_unknown_status_is_an_error_rather_than_a_guess() -> None:
    with pytest.raises(GitHubError, match="unknown GitHub file status"):
        change_type_for_status("teleported")


def test_mapping_covers_every_github_status() -> None:
    assert set(GITHUB_STATUS_TO_CHANGE_TYPE) == {
        "added",
        "removed",
        "modified",
        "renamed",
        "copied",
        "changed",
        "unchanged",
    }


# -- ChangedFile conversion ----------------------------------------------------------


def test_to_changed_file_maps_additions_and_deletions() -> None:
    changed = to_changed_file(_file(additions=7, deletions=3))

    assert changed.path == "src/service.py"
    assert changed.change_type is ChangeType.MODIFIED
    assert changed.additions == 7
    assert changed.deletions == 3
    assert changed.churn == 10
    assert changed.previous_path is None
    assert changed.patch_artifact_id is None


def test_rename_keeps_the_previous_path() -> None:
    changed = to_changed_file(
        _file(status="renamed", previous_filename="src/old_service.py", patch=None)
    )

    assert changed.change_type is ChangeType.RENAMED
    assert changed.previous_path == "src/old_service.py"


def test_rename_to_the_same_path_records_no_previous_path() -> None:
    changed = to_changed_file(
        _file(status="renamed", previous_filename="src/service.py", patch=None)
    )

    assert changed.previous_path is None


def test_blank_previous_filename_is_normalised_to_none() -> None:
    assert to_changed_file(_file(previous_filename="  ")).previous_path is None


# -- Binary detection ----------------------------------------------------------------


def test_file_with_a_patch_is_not_binary() -> None:
    assert is_binary_file(_file(patch="@@ -1 +1 @@\n-a\n+b\n")) is False


def test_patchless_modified_file_is_binary() -> None:
    assert is_binary_file(_file(patch=None)) is True


def test_patchless_pure_rename_is_not_binary() -> None:
    binary = _file(
        status="renamed",
        previous_filename="assets/logo-old.png",
        patch=None,
        additions=0,
        deletions=0,
        changes=0,
    )

    assert is_binary_file(binary) is False


def test_patchless_rename_with_line_changes_is_binary() -> None:
    binary = _file(
        status="renamed",
        previous_filename="old.txt",
        patch=None,
        additions=3,
        deletions=1,
    )

    assert is_binary_file(binary) is True


# -- ChangeSet -----------------------------------------------------------------------


def test_build_change_set_pins_both_shas() -> None:
    change_set = build_change_set(
        BASE_SHA,
        HEAD_SHA,
        [
            _file(),
            _file(
                filename="README.md",
                status="added",
                additions=1,
                deletions=0,
                patch="@@ -0,0 +1 @@\n+new\n",
            ),
        ],
        diff_artifact_id="art_01J8ZQ",
    )

    assert change_set.base_sha == BASE_SHA
    assert change_set.head_sha == HEAD_SHA
    assert change_set.diff_artifact_id == "art_01J8ZQ"
    assert change_set.changed_paths == ["src/service.py", "README.md"]
    assert change_set.total_additions == 5
    assert change_set.total_deletions == 2
    assert change_set.contains("src/")


def test_build_change_set_accepts_raw_github_json() -> None:
    change_set = build_change_set(
        BASE_SHA,
        HEAD_SHA,
        [{"filename": "app.py", "status": "removed", "additions": 0, "deletions": 9}],
    )

    assert change_set.files[0].change_type is ChangeType.DELETED
    assert change_set.files[0].deletions == 9


def test_build_change_set_is_empty_for_no_files() -> None:
    change_set = build_change_set(BASE_SHA, HEAD_SHA, [])

    assert change_set.files == []
    assert change_set.diff_artifact_id is None


# -- Unified diff rendering ----------------------------------------------------------


def test_modified_file_renders_a_git_compatible_hunk() -> None:
    rendered = render_unified_diff([_file()])

    assert rendered == (
        "diff --git a/src/service.py b/src/service.py\n"
        "--- a/src/service.py\n"
        "+++ b/src/service.py\n"
        "@@ -1,3 +1,4 @@\n"
        " context\n"
        "-removed\n"
        "+added\n"
        "+another\n"
    )


def test_added_file_uses_dev_null_and_a_mode_header() -> None:
    rendered = render_unified_diff(
        [_file(filename="docs/new.md", status="added", patch="@@ -0,0 +1 @@\n+hello\n")]
    )

    assert "new file mode 100644" in rendered
    assert "--- /dev/null" in rendered
    assert "+++ b/docs/new.md" in rendered


def test_deleted_file_uses_dev_null() -> None:
    rendered = render_unified_diff(
        [_file(filename="docs/gone.md", status="removed", patch="@@ -1 +0,0 @@\n-bye\n")]
    )

    assert "deleted file mode 100644" in rendered
    assert "--- a/docs/gone.md" in rendered
    assert "+++ /dev/null" in rendered


def test_rename_renders_rename_headers() -> None:
    rendered = render_unified_diff(
        [
            _file(
                filename="src/new_name.py",
                status="renamed",
                previous_filename="src/old_name.py",
                patch="@@ -1 +1 @@\n-x\n+y\n",
            )
        ]
    )

    assert rendered.startswith("diff --git a/src/old_name.py b/src/new_name.py\n")
    assert "rename from src/old_name.py" in rendered
    assert "rename to src/new_name.py" in rendered
    assert "--- a/src/old_name.py" in rendered
    assert "+++ b/src/new_name.py" in rendered


def test_copy_renders_copy_headers() -> None:
    rendered = render_unified_diff(
        [
            _file(
                filename="src/copy.py",
                status="copied",
                previous_filename="src/original.py",
                patch=None,
                additions=0,
                deletions=0,
                changes=0,
            )
        ]
    )

    assert "copy from src/original.py" in rendered
    assert "copy to src/copy.py" in rendered
    assert "--- a/src/original.py" in rendered


def test_binary_file_renders_the_git_binary_line() -> None:
    rendered = render_unified_diff([_file(filename="assets/logo.png", patch=None)])

    assert rendered == (
        "diff --git a/assets/logo.png b/assets/logo.png\n"
        "Binary files a/assets/logo.png and b/assets/logo.png differ\n"
    )


def test_no_newline_marker_is_preserved() -> None:
    patch = "@@ -1 +1 @@\n-old\n\\ No newline at end of file\n+new\n\\ No newline at end of file\n"

    rendered = render_unified_diff([_file(patch=patch)])

    assert "\\ No newline at end of file\n+new\n\\ No newline at end of file\n" in rendered


def test_patch_without_a_trailing_newline_still_ends_with_one() -> None:
    rendered = render_unified_diff([_file(patch="@@ -1 +1 @@\n-a\n+b")])

    assert rendered.endswith("+b\n")
    assert not rendered.endswith("\n\n")


def test_pure_rename_without_a_patch_renders_headers_only() -> None:
    rendered = render_unified_diff(
        [
            _file(
                filename="src/new.py",
                status="renamed",
                previous_filename="src/old.py",
                patch=None,
                additions=0,
                deletions=0,
                changes=0,
            )
        ]
    )

    assert rendered == (
        "diff --git a/src/old.py b/src/new.py\n"
        "rename from src/old.py\n"
        "rename to src/new.py\n"
        "--- a/src/old.py\n"
        "+++ b/src/new.py\n"
    )


def test_paths_with_spaces_are_quoted_the_way_git_quotes_them() -> None:
    rendered = render_unified_diff([_file(filename="docs/my report.md")])

    assert 'diff --git "a/docs/my report.md" "b/docs/my report.md"' in rendered
    assert '--- "a/docs/my report.md"' in rendered


def test_multiple_files_are_concatenated_in_order() -> None:
    rendered = render_unified_diff(
        [
            _file(filename="a.py", patch="@@ -1 +1 @@\n-a\n+b\n"),
            _file(filename="b.py", patch="@@ -1 +1 @@\n-c\n+d\n"),
        ]
    )

    assert rendered.count("diff --git ") == 2
    assert rendered.index("a/a.py") < rendered.index("a/b.py")
    assert rendered.endswith("+d\n")


def test_empty_file_list_renders_nothing() -> None:
    assert render_unified_diff([]) == ""
