"""Parse a real ``git diff`` into GitHub-shaped file entries.

Why this exists at all: the pinned checkout is the authority on what the pull request
changed. GitHub's own file list is convenient but can be truncated for a large pull request
(and is unavailable in an offline harness), whereas ``git diff base...head`` inside the
sandbox is exactly the three-dot comparison the report must describe.

Why GitHub-shaped entries rather than :class:`scout_domain.models.repository.ChangedFile`
directly: :func:`scout_github.build_change_set` and :func:`scout_github.render_unified_diff`
already own the mapping from GitHub's vocabulary to Scout's (including the ``removed`` ->
``DELETED`` rename that would silently corrupt a report if it were missed) and own the
git-compatible diff rendering. Rather than duplicate either, this module produces the input
those functions already accept.

Parsing rules that matter:

* ``-z`` is not used, so git may C-quote a path containing spaces or non-ASCII bytes. Quoted
  paths are unescaped rather than passed through, because a report that names ``"pa th.py"``
  is a report a reviewer cannot open.
* A binary file has no hunk and no ``---``/``+++`` pair, so its path must come from the
  ``diff --git`` header. Getting that wrong would mislabel the file as text and hand
  unreadable bytes to an agent.
* ``patch`` is left ``None`` for a binary file so the renderer emits git's own
  ``Binary files ... differ`` line, and ``""`` for a metadata-only rename so the file is not
  mistaken for a binary blob.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "DIFF_FLAGS",
    "build_diff_argv",
    "parse_git_diff",
    "render_path_list",
    "unquote_git_path",
]

#: Flags used for the change-set diff. Every one of them exists to keep the output stable:
#: rename detection so a rename is one entry rather than a delete plus an add, and no
#: colour, external diff driver, or textconv so the bytes match ``git diff`` in a terminal.
DIFF_FLAGS: Final[tuple[str, ...]] = (
    "-M",
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--unified=3",
)

#: Prefix git writes on the two sides of a diff. Stripped so the domain sees the repository
#: relative path it stores everywhere else.
_PREFIXES: Final[tuple[str, ...]] = ("a/", "b/")

_DEV_NULL: Final = "/dev/null"

_SIMPLE_ESCAPES: Final[dict[str, str]] = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    '"': '"',
    "\\": "\\",
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "v": "\v",
}


def build_diff_argv(base_sha: str, head_sha: str) -> list[str]:
    """Return the git argument vector for the three-dot change diff."""
    return ["git", "diff", *DIFF_FLAGS, f"{base_sha}...{head_sha}"]


def parse_git_diff(text: str) -> list[dict[str, Any]]:
    """Translate a unified diff into GitHub-shaped file entries.

    Returns:
        One mapping per changed file, in the order git printed them, each with ``filename``,
        ``status`` (GitHub's vocabulary), ``previous_filename``, ``additions``, ``deletions``, and
        ``patch``.
    """
    entries: list[dict[str, Any]] = []
    current: _EntryBuilder | None = None

    for line in text.splitlines():
        if line.startswith("diff --git "):
            if current is not None:
                entries.append(current.finish())
            current = _EntryBuilder.for_header(line[len("diff --git ") :])
            continue
        if current is None:
            continue
        current.consume(line)

    if current is not None:
        entries.append(current.finish())
    return entries


@dataclass
class _EntryBuilder:
    """Accumulates one file's section of a diff.

    A builder rather than a long function: the per-line state machine has three independent
    concerns (metadata headers, the two sides, and the hunk body plus its line counts), and keeping
    each in its own small method is what makes the parsing rules reviewable.
    """

    filename: str
    status: str = "modified"
    previous_filename: str | None = None
    additions: int = 0
    deletions: int = 0
    binary: bool = False
    hunk: list[str] = field(default_factory=list)
    in_hunk: bool = False

    @classmethod
    def for_header(cls, header: str) -> _EntryBuilder:
        """Build a builder from ``diff --git <old> <new>``, before any hunk is seen."""
        tokens = _split_header_tokens(header)
        left = unquote_git_path(tokens[0]) if tokens else ""
        right = unquote_git_path(tokens[1]) if len(tokens) > 1 else left
        filename = _strip_prefix(right) or _strip_prefix(left) or right or left
        previous = _strip_prefix(left)
        return cls(
            filename=filename,
            previous_filename=None if previous in {"", filename} else previous,
        )

    def consume(self, line: str) -> None:
        """Apply one diff line to this entry."""
        if _consume_metadata(self, line):
            return
        if _consume_side(self, line):
            return
        _consume_hunk(self, line)

    def finish(self) -> dict[str, Any]:
        """Return the GitHub-shaped entry.

        ``patch`` is ``None`` for a binary file so the renderer emits git's own
        ``Binary files ... differ`` line, and ``""`` for a metadata-only rename so the file is not
        mistaken for a binary blob.
        """
        return {
            "filename": self.filename,
            "status": self.status,
            "previous_filename": self.previous_filename,
            "additions": self.additions,
            "deletions": self.deletions,
            "patch": None if self.binary else "\n".join(self.hunk),
        }


def _consume_metadata(entry: _EntryBuilder, line: str) -> bool:
    """Handle the mode, rename, copy, and binary headers. Returns whether the line was consumed."""
    if line.startswith("new file mode"):
        entry.status = "added"
    elif line.startswith("deleted file mode"):
        entry.status = "removed"
    elif line.startswith("rename from "):
        entry.previous_filename = unquote_git_path(line[len("rename from ") :])
        entry.status = "renamed"
    elif line.startswith("rename to "):
        entry.filename = unquote_git_path(line[len("rename to ") :])
        entry.status = "renamed"
    elif line.startswith("copy from "):
        entry.previous_filename = unquote_git_path(line[len("copy from ") :])
        entry.status = "copied"
    elif line.startswith("copy to "):
        entry.filename = unquote_git_path(line[len("copy to ") :])
        entry.status = "copied"
    elif line.startswith("Binary files "):
        entry.binary = True
        entry.in_hunk = False
    else:
        return False
    entry.in_hunk = False
    return True


def _consume_side(entry: _EntryBuilder, line: str) -> bool:
    """Handle a ``---``/``+++`` side line. Returns whether the line was consumed.

    The old side determines nothing on its own: a deleted file is identified by the
    ``deleted file mode`` header above plus its ``+++ /dev/null`` side.
    """
    if line.startswith("+++ "):
        new_path = _side_path(line[4:])
        if new_path is not None:
            entry.filename = new_path
    elif not line.startswith("--- "):
        return False
    entry.in_hunk = False
    return True


def _consume_hunk(entry: _EntryBuilder, line: str) -> None:
    """Handle an ``@@`` header or a hunk body line, counting added and removed lines."""
    if line.startswith("@@"):
        entry.in_hunk = True
        entry.hunk.append(line)
        return
    if not entry.in_hunk:
        return
    entry.hunk.append(line)
    if line.startswith("+") and not line.startswith("+++"):
        entry.additions += 1
    elif line.startswith("-") and not line.startswith("---"):
        entry.deletions += 1


def unquote_git_path(token: str) -> str:
    """Undo git's C-style path quoting.

    Git wraps a path in double quotes and escapes whitespace-adjacent control characters and
    (with the default ``core.quotePath``) every non-ASCII byte as an octal escape. Undoing
    that here means a finding points at a path a reviewer can copy into an editor.
    """
    candidate = token.strip()
    if not candidate.startswith('"'):
        return candidate
    if not candidate.endswith('"') or len(candidate) < 2:
        return candidate.strip('"')

    inner = candidate[1:-1]
    out = bytearray()
    index = 0
    length = len(inner)
    while index < length:
        char = inner[index]
        if char == "\\" and index + 1 < length:
            following = inner[index + 1]
            if following in "01234567" and index + 3 < length:
                out.append(int(inner[index + 1 : index + 4], 8))
                index += 4
                continue
            replacement = _SIMPLE_ESCAPES.get(following)
            if replacement is not None:
                out.extend(replacement.encode("utf-8"))
                index += 2
                continue
            out.extend(following.encode("utf-8"))
            index += 2
            continue
        out.extend(char.encode("utf-8"))
        index += 1
    return out.decode("utf-8", errors="replace")


def _split_header_tokens(header: str) -> list[str]:
    """Split a ``diff --git`` header into its (at most two) path tokens.

    Whitespace separates the tokens only outside quotes, because an unquoted path cannot
    contain a space in git's own output -- which is precisely why git quotes such paths.
    """
    tokens: list[str] = []
    buffer: list[str] = []
    quoted = False
    escaped = False
    for char in header.strip():
        if escaped:
            buffer.append(char)
            escaped = False
            continue
        if quoted and char == "\\":
            buffer.append(char)
            escaped = True
            continue
        if char == '"':
            quoted = not quoted
            buffer.append(char)
            continue
        if char.isspace() and not quoted:
            if buffer:
                tokens.append("".join(buffer))
                buffer = []
            continue
        buffer.append(char)
    if buffer:
        tokens.append("".join(buffer))
    return tokens


def _side_path(value: str) -> str | None:
    """Return the repository-relative path from a ``---``/``+++`` side, or ``None``."""
    candidate = unquote_git_path(value.split("\t", 1)[0])
    if candidate == _DEV_NULL:
        return None
    return _strip_prefix(candidate) or candidate


def _strip_prefix(path: str) -> str:
    """Remove git's ``a/``/``b/`` prefix, leaving the repository-relative path."""
    for prefix in _PREFIXES:
        if path.startswith(prefix):
            return path[len(prefix) :]
    return path


def render_path_list(paths: Sequence[str]) -> str:
    """Render a changed-path list as the newline-separated artifact body."""
    return "\n".join(paths) + "\n" if paths else ""
