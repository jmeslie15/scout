"""GitHub diff payloads to Scout change sets, plus faithful unified-diff rendering.

Two jobs live here, both on the path from "GitHub says these files changed" to a pinned,
reviewable :class:`~scout_domain.models.repository.ChangeSet` (spec section 16):

* :func:`build_change_set` translates GitHub's vocabulary into Scout's. The important
  detail is that GitHub says ``removed`` where Scout says ``DELETED``; a wrong mapping
  changes the risk classification of the whole change, so an unknown status is an error
  rather than a guess.
* :func:`render_unified_diff` reconstructs a git-compatible unified diff for storage as
  raw evidence. GitHub returns only the hunk body, so the file headers, mode headers, and
  rename headers are synthesised. ``index`` lines are deliberately omitted: GitHub's file
  entries expose a single blob SHA, and inventing the other one would be a fabrication
  inside an evidence artefact (Rule 15).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

from scout_domain.models.repository import ChangedFile, ChangeSet, ChangeType
from scout_github.errors import GitHubError
from scout_github.models import GitHubFile

__all__ = [
    "GITHUB_STATUS_TO_CHANGE_TYPE",
    "build_change_set",
    "change_type_for_status",
    "is_binary_file",
    "render_unified_diff",
    "to_changed_file",
]

#: GitHub ``status`` to Scout :class:`ChangeType`.
#:
#: ``removed`` maps to ``DELETED`` — the one renaming that would silently corrupt a
#: report if it were missed. ``changed`` (content or mode changed) and ``unchanged``
#: (present in the list but byte-identical, which happens for a file whose mode changed
#: back) both map to ``MODIFIED``: Scout's vocabulary has no better word, and neither is
#: a deletion or an addition. ``UNMERGED`` and ``TYPE_CHANGED`` have no GitHub
#: counterpart: they only arise from a local git working tree, never from the API.
GITHUB_STATUS_TO_CHANGE_TYPE: Final[Mapping[str, ChangeType]] = {
    "added": ChangeType.ADDED,
    "removed": ChangeType.DELETED,
    "modified": ChangeType.MODIFIED,
    "renamed": ChangeType.RENAMED,
    "copied": ChangeType.COPIED,
    "changed": ChangeType.MODIFIED,
    "unchanged": ChangeType.MODIFIED,
}

#: Statuses whose patch is legitimately absent without the file being binary.
_PATCHLESS_METADATA_STATUSES: Final[frozenset[str]] = frozenset({"renamed", "copied"})

#: Paths that git quotes (spaces, control characters, non-ASCII) plus the C escapes.
_PATH_NEEDS_QUOTING: Final = re.compile(r'[\s"\\\x00-\x1f\x7f]|[^\x20-\x7e]')

#: Placeholder used in place of a side that does not exist (added/deleted files).
_DEV_NULL: Final = "/dev/null"


def change_type_for_status(status: str) -> ChangeType:
    """Map GitHub's file ``status`` string onto Scout's :class:`ChangeType`.

    Why this raises instead of defaulting: the status drives risk classification and the
    "was anything deleted?" question a reviewer asks first. Falling back to ``MODIFIED``
    for an unrecognised value would turn a deletion — or a future GitHub status — into a
    benign-looking edit, and nothing downstream could detect the loss.

    Args:
        status: GitHub's status string, e.g. ``"removed"``.

    Returns:
        The corresponding :class:`ChangeType`.

    Raises:
        GitHubError: When the status is not one GitHub documents for compare/PR files.
    """
    normalized = status.strip().lower()
    try:
        return GITHUB_STATUS_TO_CHANGE_TYPE[normalized]
    except KeyError as exc:
        raise GitHubError(
            f"unknown GitHub file status {status!r}; Scout cannot classify this change",
            status=status,
            known_statuses=sorted(GITHUB_STATUS_TO_CHANGE_TYPE),
        ) from exc


def is_binary_file(file: GitHubFile) -> bool:
    """Return ``True`` when a file entry has no reviewable text patch.

    Why this is subtler than "patch is None": GitHub omits ``patch`` for binary content,
    *but also* for a rename or copy that changed no bytes, and for diffs it considers too
    large. Treating every patch-less file as binary would mislabel a pure rename as a
    binary blob; treating none as binary would hand unreadable bytes to a review agent.
    The rule adopted here: a patch-less rename or copy with zero added and zero removed
    lines is a metadata-only change and is not binary; every other patch-less entry is
    not text-reviewable and is reported as binary, which is the conservative choice for
    AI review.
    """
    if file.patch is not None:
        return False
    metadata_only = (
        file.status.strip().lower() in _PATCHLESS_METADATA_STATUSES
        and file.additions == 0
        and file.deletions == 0
    )
    return not metadata_only


def to_changed_file(file: GitHubFile) -> ChangedFile:
    """Convert one GitHub file entry into Scout's :class:`ChangedFile`.

    ``patch_artifact_id`` is left unset: per-file patches are stored by the caller, which
    owns the artifact store. Inlining patch text into the domain object is exactly what
    spec section 21 forbids.

    Args:
        file: A file entry from a PR-files or compare response.

    Returns:
        The domain representation, with ``previous_path`` populated for renames and
        copies.
    """
    change_type = change_type_for_status(file.status)
    previous_path = (file.previous_filename or "").strip() or None
    if previous_path == file.filename:
        # A rename to the same path is not a rename; recording one would produce a
        # confusing "renamed x -> x" line in the report.
        previous_path = None

    return ChangedFile(
        path=file.filename,
        change_type=change_type,
        previous_path=previous_path,
        additions=file.additions,
        deletions=file.deletions,
        is_binary=is_binary_file(file),
    )


def build_change_set(
    base_sha: str,
    head_sha: str,
    files: Sequence[GitHubFile | Mapping[str, Any]],
    *,
    diff_artifact_id: str | None = None,
) -> ChangeSet:
    """Build Scout's pinned :class:`ChangeSet` from GitHub file entries.

    Accepts GitHub's raw JSON entries as well as typed models, because the API layer
    often has the parsed JSON in hand and re-validating it here keeps the mapping in one
    place.

    Args:
        base_sha: Base commit of the comparison. Pinned, never a branch name.
        head_sha: Head commit of the comparison. Pinned, never a branch name.
        files: File entries from ``compare_commits`` or ``list_pull_request_files``.
        diff_artifact_id: Artifact id holding the full unified diff, when the caller has
            already stored one.

    Returns:
        A :class:`ChangeSet` whose ``files`` are in GitHub's order (GitHub sorts by
        path, which keeps the report stable between runs).
    """
    return ChangeSet(
        base_sha=base_sha,
        head_sha=head_sha,
        files=[to_changed_file(_coerce_file(file)) for file in files],
        diff_artifact_id=diff_artifact_id,
    )


def render_unified_diff(files: Sequence[GitHubFile | Mapping[str, Any]]) -> str:
    """Render a git-compatible unified diff for storage as an artifact.

    The output is *evidence*: it is shown to humans, diffed again later, and attached to
    a report. It therefore reproduces what ``git diff`` prints rather than a prettified
    summary — same ``diff --git`` header, same ``---``/``+++`` sides, same
    ``\\ No newline at end of file`` markers, same binary-file line, and the same
    C-style path quoting git applies by default.

    Args:
        files: File entries from ``compare_commits`` or ``list_pull_request_files``.

    Returns:
        A unified diff whose concatenated per-file sections each end with exactly one
        newline. Empty when ``files`` is empty.
    """
    return "".join(_render_file_diff(_coerce_file(file)) for file in files)


def _coerce_file(file: GitHubFile | Mapping[str, Any]) -> GitHubFile:
    """Accept either a typed file entry or GitHub's raw JSON mapping."""
    if isinstance(file, GitHubFile):
        return file
    return GitHubFile.model_validate(file)


def _render_file_diff(file: GitHubFile) -> str:
    """Render one file's section of a unified diff."""
    change_type = change_type_for_status(file.status)
    path = file.filename
    previous_path = (file.previous_filename or "").strip() or path

    is_rename_like = change_type in {ChangeType.RENAMED, ChangeType.COPIED}
    left_path = previous_path if is_rename_like else path
    right_path = path

    lines: list[str] = [
        f"diff --git {_quote_path(f'a/{left_path}')} {_quote_path(f'b/{right_path}')}"
    ]

    if change_type is ChangeType.ADDED:
        lines.append("new file mode 100644")
    elif change_type is ChangeType.DELETED:
        lines.append("deleted file mode 100644")
    elif change_type is ChangeType.COPIED:
        lines.append(f"copy from {_quote_path(previous_path)}")
        lines.append(f"copy to {_quote_path(path)}")
    elif change_type is ChangeType.RENAMED:
        lines.append(f"rename from {_quote_path(previous_path)}")
        lines.append(f"rename to {_quote_path(path)}")

    if is_binary_file(file):
        lines.append(
            f"Binary files {_quote_path(f'a/{left_path}')} and "
            f"{_quote_path(f'b/{right_path}')} differ"
        )
        return "\n".join(lines) + "\n"

    left_side = _DEV_NULL if change_type is ChangeType.ADDED else _quote_path(f"a/{left_path}")
    right_side = _DEV_NULL if change_type is ChangeType.DELETED else _quote_path(f"b/{right_path}")
    lines.append(f"--- {left_side}")
    lines.append(f"+++ {right_side}")

    # GitHub's hunk body already contains the trailing "\ No newline at end of file"
    # marker when it applies. Normalising only the final newline preserves that marker
    # instead of appending a stray line after it.
    patch = (file.patch or "").rstrip("\n")
    if patch:
        lines.append(patch)

    return "\n".join(lines) + "\n"


def _quote_path(path: str) -> str:
    """Quote a path the way ``git diff`` does with ``core.quotePath`` at its default.

    Git wraps a path in double quotes and escapes tabs, newlines, backslashes, quotes, and
    (by default) every non-ASCII byte as an octal escape. Reproducing that keeps the
    stored diff byte-comparable with one produced by ``git diff`` for the same change.
    """
    if not _PATH_NEEDS_QUOTING.search(path):
        return path

    out: list[str] = []
    for char in path:
        if char == "\\":
            out.append("\\\\")
        elif char == '"':
            out.append('\\"')
        elif char == "\t":
            out.append("\\t")
        elif char == "\n":
            out.append("\\n")
        elif char == "\r":
            out.append("\\r")
        elif " " <= char <= "~":
            out.append(char)
        else:
            out.append(f"\\{ord(char):03o}")
    return '"' + "".join(out) + '"'
