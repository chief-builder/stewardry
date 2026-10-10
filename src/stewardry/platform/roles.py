"""Active role assignments (ADR 0008: roles live in Stewardry, not in the hub)."""

from sqlalchemy import Connection, select

from stewardry.platform.db.schema import role_assignments


def active_roles(conn: Connection, sub: str) -> set[str]:
    rows = conn.execute(
        select(role_assignments.c.role).where(
            role_assignments.c.sub == sub, role_assignments.c.revoked_at.is_(None)
        )
    )
    return set(rows.scalars())
