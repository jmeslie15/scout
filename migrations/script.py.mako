"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

Scout note: migrations are reviewed like code. The generated body is a starting point,
not a deliverable — check that a new constraint is named, that a dropped column is
intentional, and that ``downgrade()`` really reverses ``upgrade()``. ``alembic check``
is what proves the result matches the models.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}
# Identifier for this revision, and the revision it applies on top of. ``None`` in
# ``down_revision`` means this is the initial migration.
revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    """Apply this revision."""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """Reverse this revision.

    Every migration Scout ships must be reversible: ``alembic downgrade base`` followed
    by ``upgrade head`` is part of the verification gate, and a migration whose
    downgrade is a stub makes that gate a lie.
    """
    ${downgrades if downgrades else "pass"}
