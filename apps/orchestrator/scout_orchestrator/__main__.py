"""``python -m scout_orchestrator``.

A separate module so ``--help`` and ``--version`` work without starting anything: the entry point
only parses arguments and delegates, and a non-zero exit code is propagated to the shell.
"""

from __future__ import annotations

import sys

from scout_orchestrator.main import main

if __name__ == "__main__":
    sys.exit(main())
