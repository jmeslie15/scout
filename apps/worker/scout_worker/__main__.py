"""``python -m scout_worker``.

A separate module so ``--help`` and ``--version`` work without starting anything.
"""

from __future__ import annotations

import sys

from scout_worker.main import main

if __name__ == "__main__":
    sys.exit(main())
