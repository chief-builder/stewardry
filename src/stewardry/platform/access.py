"""Access mapping: users, roles, and customer assignments (ADR 0008).

The hub only authenticates people. Who may do what in Stewardry comes from
this mapping, which a platform engineer applies with `stewardry roles apply`.
Hub `groups` claims are never read for authority.

The mapping is declarative: after an apply, active roles and customer
assignments match the file exactly. Users missing from the file keep their
row (history refers to it) but lose every role and customer.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import Connection, Engine, insert, select, update

from stewardry.platform.audit import AuditEvent, AuditLog, record_now
from stewardry.platform.db.schema import (
    ROLES,
    customer_assignments,
    customers,
    role_assignments,
    users,
    utcnow,
)
from stewardry.platform.roles import active_roles

PLATFORM_ENGINEER = "platform_engineer"


class AccessError(Exception):
    """An access request was refused. The refusal has been audited."""


class SignInRefused(AccessError):
    pass


def _known_role(value: str) -> str:
    if value not in ROLES:
        raise ValueError(f"unknown role {value!r}; expected one of {ROLES}")
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CustomerEntry(_Strict):
    customer_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    display_name: str = Field(min_length=1, max_length=255)


class UserEntry(_Strict):
    username: str = Field(min_length=1, max_length=255, pattern=r"^[^~\s]+$")
    sub: str = Field(min_length=1, max_length=255)
    display_name: str = Field(min_length=1, max_length=255)
    roles: list[Annotated[str, AfterValidator(_known_role)]]
    customers: list[str] = Field(default_factory=list)


class AccessMapping(_Strict):
    schema_version: int = Field(ge=1, le=1)
    customers: list[CustomerEntry]
    users: list[UserEntry]

    @model_validator(mode="after")
    def _consistent(self) -> AccessMapping:
        for field in ("username", "sub"):
            values = [getattr(u, field) for u in self.users]
            if len(values) != len(set(values)):
                raise ValueError(f"each user needs a unique {field}")
        known = {c.customer_id for c in self.customers}
        for user in self.users:
            if missing := set(user.customers) - known:
                raise ValueError(f"user {user.username} names unknown customers {sorted(missing)}")
        if not any(PLATFORM_ENGINEER in u.roles for u in self.users):
            raise ValueError("the mapping must keep at least one platform_engineer")
        return self


def load_access_mapping(path: Path) -> AccessMapping:
    try:
        return AccessMapping.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise AccessError(f"{path}: {exc}") from exc


@dataclass(frozen=True)
class ApplySummary:
    users_added: tuple[str, ...]
    users_remapped: tuple[str, ...]
    users_unmapped: tuple[str, ...]
    roles_assigned: tuple[str, ...]
    roles_revoked: tuple[str, ...]
    customers_assigned: tuple[str, ...]
    customers_revoked: tuple[str, ...]


def _refuse(engine: Engine, log: AuditLog, actor_sub: str, reason: str, outcome: str) -> None:
    record_now(
        engine,
        log,
        AuditEvent(
            "access.mapping_applied", outcome, actor_sub=actor_sub, details={"reason": reason}
        ),
    )
    raise AccessError(reason)


def _any_platform_engineer(conn: Connection) -> bool:
    return (
        conn.execute(
            select(role_assignments.c.id).where(
                role_assignments.c.role == PLATFORM_ENGINEER,
                role_assignments.c.revoked_at.is_(None),
            )
        ).first()
        is not None
    )


def _sync(
    conn: Connection,
    table: Any,
    column: str,
    sub: str,
    desired: set[str],
    actor_sub: str,
    label: str,
) -> tuple[list[str], list[str]]:
    """Make one user's active assignments in `table` equal `desired`."""
    value_col = table.c[column]
    current: set[str] = set(
        conn.execute(
            select(value_col).where(table.c.sub == sub, table.c.revoked_at.is_(None))
        ).scalars()
    )
    now = utcnow()
    for value in sorted(desired - current):
        conn.execute(
            insert(table).values(
                {"sub": sub, column: value, "assigned_by_sub": actor_sub, "assigned_at": now}
            )
        )
    for value in sorted(current - desired):
        conn.execute(
            update(table)
            .where(table.c.sub == sub, value_col == value, table.c.revoked_at.is_(None))
            .values(revoked_at=now)
        )
    return (
        [f"{label}:{v}" for v in sorted(desired - current)],
        [f"{label}:{v}" for v in sorted(current - desired)],
    )


def apply_access_mapping(
    engine: Engine,
    log: AuditLog,
    actor_sub: str,
    mapping: AccessMapping,
    *,
    bootstrap: bool = False,
    remap: frozenset[str] = frozenset(),
) -> ApplySummary:
    """Apply the mapping in one transaction.

    `bootstrap` is allowed only while no active platform engineer exists, and
    only if the mapping makes the actor one. `remap` names usernames whose hub
    subject is allowed to change (ADR 0008); the old user row keeps its history
    under a retired username.
    """
    by_sub = {u.sub: u for u in mapping.users}
    with engine.connect() as conn:
        has_engineer = _any_platform_engineer(conn)
        actor_roles = active_roles(conn, actor_sub)
        existing = {r.sub: r for r in conn.execute(select(users))}
    by_username = {r.username: r for r in existing.values()}

    if bootstrap:
        if has_engineer:
            _refuse(
                engine,
                log,
                actor_sub,
                "bootstrap is only allowed before any platform engineer exists",
                "denied",
            )
        if PLATFORM_ENGINEER not in getattr(by_sub.get(actor_sub), "roles", []):
            _refuse(
                engine,
                log,
                actor_sub,
                "bootstrap must make the acting user a platform_engineer",
                "denied",
            )
    elif PLATFORM_ENGINEER not in actor_roles:
        _refuse(engine, log, actor_sub, "requires the platform_engineer role", "denied")

    remapped = []
    for entry in mapping.users:
        old = by_username.get(entry.username)
        if old is not None and old.sub != entry.sub:
            if entry.username not in remap:
                _refuse(
                    engine,
                    log,
                    actor_sub,
                    f"username {entry.username} now has a different hub subject; "
                    f"re-map it explicitly with --remap {entry.username}",
                    "failed",
                )
            remapped.append(entry.username)

    added: list[str] = []
    assigned: list[str] = []
    revoked: list[str] = []
    cust_assigned: list[str] = []
    cust_revoked: list[str] = []
    now = utcnow()
    with engine.begin() as conn:
        for customer in mapping.customers:
            row = conn.execute(
                select(customers).where(customers.c.customer_id == customer.customer_id)
            ).first()
            if row is None:
                conn.execute(insert(customers).values(**customer.model_dump()))
            elif row.display_name != customer.display_name:
                conn.execute(
                    update(customers)
                    .where(customers.c.customer_id == customer.customer_id)
                    .values(display_name=customer.display_name)
                )

        for username in remapped:
            old = by_username[username]
            retired_name = f"{username}~{old.sub[:12]}"
            conn.execute(update(users).where(users.c.sub == old.sub).values(username=retired_name))
            for table, column, label in (
                (role_assignments, "role", "role"),
                (customer_assignments, "customer_id", "customer"),
            ):
                _, gone = _sync(conn, table, column, old.sub, set(), actor_sub, label)
                revoked.extend(f"{retired_name}/{g}" for g in gone)
            log.append(
                conn,
                AuditEvent(
                    "access.user_remapped",
                    "succeeded",
                    actor_sub=actor_sub,
                    details={
                        "username": username,
                        "old_sub": old.sub,
                        "new_sub": next(u.sub for u in mapping.users if u.username == username),
                        "old_row_renamed_to": retired_name,
                    },
                ),
            )

        for entry in mapping.users:
            if entry.sub not in existing:
                conn.execute(
                    insert(users).values(
                        sub=entry.sub,
                        username=entry.username,
                        display_name=entry.display_name,
                        created_at=now,
                    )
                )
                added.append(entry.username)
            elif (existing[entry.sub].username, existing[entry.sub].display_name) != (
                entry.username,
                entry.display_name,
            ):
                conn.execute(
                    update(users)
                    .where(users.c.sub == entry.sub)
                    .values(
                        username=entry.username,
                        display_name=entry.display_name,
                    )
                )

        unmapped: list[str] = []
        for sub in [*by_sub, *(s for s in existing if s not in by_sub)]:
            mapped = by_sub.get(sub)
            name = mapped.username if mapped else existing[sub].username
            roles_want = set(mapped.roles) if mapped else set()
            customers_want = set(mapped.customers) if mapped else set()
            got, roles_gone = _sync(
                conn, role_assignments, "role", sub, roles_want, actor_sub, "role"
            )
            assigned.extend(f"{name}/{g}" for g in got)
            revoked.extend(f"{name}/{g}" for g in roles_gone)
            got, customers_gone = _sync(
                conn,
                customer_assignments,
                "customer_id",
                sub,
                customers_want,
                actor_sub,
                "customer",
            )
            cust_assigned.extend(f"{name}/{g}" for g in got)
            cust_revoked.extend(f"{name}/{g}" for g in customers_gone)
            if mapped is None and (roles_gone or customers_gone):
                unmapped.append(name)

        summary = ApplySummary(
            tuple(added),
            tuple(remapped),
            tuple(sorted(set(unmapped))),
            tuple(assigned),
            tuple(revoked),
            tuple(cust_assigned),
            tuple(cust_revoked),
        )
        log.append(
            conn,
            AuditEvent(
                "access.mapping_applied",
                "succeeded",
                actor_sub=actor_sub,
                details={
                    "bootstrap": bootstrap,
                    **{k: list(v) for k, v in summary.__dict__.items()},
                },
            ),
        )
    return summary


# ------------------------------------------------------------------ sign-in


@dataclass(frozen=True)
class SignedInUser:
    sub: str
    username: str
    roles: frozenset[str]
    customers: frozenset[str]


def assigned_customers(conn: Connection, sub: str) -> set[str]:
    rows = conn.execute(
        select(customer_assignments.c.customer_id).where(
            customer_assignments.c.sub == sub, customer_assignments.c.revoked_at.is_(None)
        )
    )
    return set(rows.scalars())


def resolve_sign_in(engine: Engine, log: AuditLog, sub: str, username: str) -> SignedInUser:
    """Decide whether a hub-authenticated person may use Stewardry.

    Only the hub subject and username are used; group claims never are.
    Refusals are audited as denied sign-ins.
    """
    with engine.connect() as conn:
        by_sub = conn.execute(select(users).where(users.c.sub == sub)).first()
        by_name = conn.execute(select(users).where(users.c.username == username)).first()
        roles = active_roles(conn, sub) if by_sub else set()
        assigned = assigned_customers(conn, sub) if by_sub else set()
    reason: str | None = None
    if by_sub is None and by_name is not None:
        reason = (
            f"username {username} is mapped to a different hub subject; "
            "a platform engineer must re-map it"
        )
    elif by_sub is None:
        reason = "this hub user is not provisioned in Stewardry"
    elif not roles:
        reason = "this user has no roles in Stewardry"
    if reason is not None or by_sub is None:
        record_now(
            engine,
            log,
            AuditEvent(
                "auth.sign_in",
                "denied",
                actor_sub=sub,
                details={"username": username, "reason": reason},
            ),
        )
        raise SignInRefused(reason or "not provisioned")
    record_now(
        engine,
        log,
        AuditEvent(
            "auth.sign_in", "succeeded", actor_sub=sub, details={"username": by_sub.username}
        ),
    )
    return SignedInUser(sub, by_sub.username, frozenset(roles), frozenset(assigned))


def list_people(conn: Connection) -> list[SignedInUser]:
    """Every user with their active roles and customers, by username."""
    people = conn.execute(select(users).order_by(users.c.username)).all()
    return [
        SignedInUser(
            p.sub,
            p.username,
            frozenset(active_roles(conn, p.sub)),
            frozenset(assigned_customers(conn, p.sub)),
        )
        for p in people
    ]
