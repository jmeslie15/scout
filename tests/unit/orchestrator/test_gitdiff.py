"""Parsing a real ``git diff`` into GitHub-shaped change entries.

The parser is the boundary between the pinned checkout and everything the report claims about the
change, so its edge cases are worth pinning: a rename must be one entry rather than a delete plus an
add, a binary file must keep ``patch=None`` so the renderer emits git's own binary line, and a
quoted path must be unescaped so a finding can point at a file a reviewer can open.
"""

from __future__ import annotations

from scout_github import build_change_set, render_unified_diff
from scout_orchestrator.stages.gitdiff import (
    build_diff_argv,
    parse_git_diff,
    render_path_list,
    unquote_git_path,
)

MODIFIED = """diff --git a/widget.py b/widget.py
index 4693ad3..3581473 100644
--- a/widget.py
+++ b/widget.py
@@ -1,2 +1,6 @@
 def add(a, b):
     return a + b
+
+
+def multiply(a, b):
+    return a * b
"""

ADDED = """diff --git a/package.json b/package.json
new file mode 100644
index 0000000..1111111
--- /dev/null
+++ b/package.json
@@ -0,0 +1,3 @@
+{
+  "name": "widget"
+}
"""

DELETED = """diff --git a/legacy.py b/legacy.py
deleted file mode 100644
index 2222222..0000000
--- a/legacy.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def old():
-    pass
"""

RENAMED = """diff --git a/old_name.py b/new_name.py
similarity index 100%
rename from old_name.py
rename to new_name.py
"""

BINARY = """diff --git a/logo.png b/logo.png
index 3333333..4444444 100644
Binary files a/logo.png and b/logo.png differ
"""

QUOTED = """diff --git "a/my file.py" "b/my file.py"
index 5555555..6666666 100644
--- "a/my file.py"
+++ "b/my file.py"
@@ -1 +1 @@
-old
+new
"""


def test_modified_file_records_the_hunk_and_the_line_counts() -> None:
    """A modified file keeps its hunk body and counts additions and deletions."""
    entries = parse_git_diff(MODIFIED)

    assert len(entries) == 1
    entry = entries[0]
    assert entry["filename"] == "widget.py"
    assert entry["status"] == "modified"
    assert entry["additions"] == 4
    assert entry["deletions"] == 0
    assert entry["patch"].startswith("@@ -1,2 +1,6 @@")


def test_added_and_deleted_files_are_distinguished() -> None:
    """An added file is ``added`` and a deleted one is ``removed`` in GitHub's vocabulary."""
    added = parse_git_diff(ADDED)[0]
    deleted = parse_git_diff(DELETED)[0]

    assert (added["filename"], added["status"]) == ("package.json", "added")
    assert (deleted["filename"], deleted["status"]) == ("legacy.py", "removed")
    assert deleted["deletions"] == 2


def test_a_rename_is_one_entry() -> None:
    """Rename detection keeps a rename as a single entry with its previous path."""
    entry = parse_git_diff(RENAMED)[0]

    assert entry["filename"] == "new_name.py"
    assert entry["status"] == "renamed"
    assert entry["previous_filename"] == "old_name.py"
    # A metadata-only rename keeps an empty (not None) patch, so it is not mistaken for a binary.
    assert entry["patch"] == ""


def test_a_binary_file_keeps_patch_none() -> None:
    """``patch=None`` is what makes the renderer emit git's binary line rather than a fake hunk."""
    entry = parse_git_diff(BINARY)[0]

    assert entry["filename"] == "logo.png"
    assert entry["patch"] is None


def test_multiple_files_are_parsed_in_order() -> None:
    """The change set preserves git's order, which keeps reports stable between runs."""
    entries = parse_git_diff(ADDED + MODIFIED + DELETED)

    assert [entry["filename"] for entry in entries] == ["package.json", "widget.py", "legacy.py"]


def test_quoted_paths_are_unescaped() -> None:
    """A path git quoted is unescaped so a finding points at a path a reviewer can open."""
    entry = parse_git_diff(QUOTED)[0]

    assert entry["filename"] == "my file.py"
    assert unquote_git_path('"a\\tb.py"') == "a\tb.py"
    assert unquote_git_path('"octal\\040space.py"') == "octal space.py"
    assert unquote_git_path("plain.py") == "plain.py"


def test_entries_feed_build_change_set_and_render_unified_diff() -> None:
    """The parser's output is exactly what the GitHub helpers consume."""
    entries = parse_git_diff(ADDED + MODIFIED + RENAMED + BINARY)
    change_set = build_change_set("a" * 40, "b" * 40, entries)

    assert [file.path for file in change_set.files] == [
        "package.json",
        "widget.py",
        "new_name.py",
        "logo.png",
    ]
    assert change_set.files[0].change_type.value == "added"
    assert change_set.files[2].previous_path == "old_name.py"
    assert change_set.files[3].is_binary is True
    assert change_set.total_additions == 7

    rendered = render_unified_diff(entries)
    assert rendered.startswith("diff --git a/package.json b/package.json")
    assert "Binary files a/logo.png and b/logo.png differ" in rendered


def test_diff_argv_requests_no_color_and_rename_detection() -> None:
    """The argv is stable: colour or a textconv driver would change the stored evidence."""
    argv = build_diff_argv("a" * 40, "b" * 40)

    assert argv[0] == "git"
    assert "--no-color" in argv
    assert "--no-ext-diff" in argv
    assert "-M" in argv
    assert argv[-1] == f"{'a' * 40}...{'b' * 40}"


def test_render_path_list_is_newline_terminated() -> None:
    """The changed-paths artifact is line-oriented text, empty when nothing changed."""
    assert render_path_list(["a.py", "b.py"]) == "a.py\nb.py\n"
    assert render_path_list([]) == ""


def test_an_empty_diff_produces_no_entries() -> None:
    """An empty range is information, not a parse failure."""
    assert parse_git_diff("") == []
