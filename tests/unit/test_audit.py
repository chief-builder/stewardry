"""Audit log: chain construction, database guards, redaction, and tamper detection."""

import json
import stat
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, select, text
from sqlalchemy.exc import IntegrityError

from stewardry.platform.audit import (
    AuditEvent,
    AuditLog,
    event_hash,
    read_anchors,
    verify,
    write_anchor,
)
from stewardry.platform.db.schema import GENESIS_HASH, audit_events
from stewardry.platform.redaction import Redactor

JWT = "eyJ" + "a" * 20 + ".eyJ" + "b" * 20 + "." + "c" * 20
CONFIGURED = "hub-client-secret-value"
TRIGGERS = ("audit_events_no_update", "audit_events_no_delete", "audit_events_extend_head")


def write_events(engine: Engine, count: int, log: AuditLog | None = None) -> None:
    log = log or AuditLog(Redactor())
    with engine.begin() as conn:
        for i in range(count):
            log.append(
                conn,
                AuditEvent(
                    "tool.call",
                    "succeeded",
                    actor_sub="sub-bob",
                    task_id="t1",
                    details={"n": i},
                ),
            )


def as_admin(conn: Connection) -> None:
    """Simulate a local administrator: remove the database guards."""
    for name in TRIGGERS:
        conn.execute(text(f"DROP TRIGGER {name}"))


def rows(conn: Connection) -> list[dict[str, object]]:
    result = conn.execute(text("SELECT * FROM audit_events ORDER BY seq")).mappings()
    return [dict(r) for r in result]


def insert_raw(conn: Connection, row: dict[str, object]) -> None:
    """Insert a row exactly as given (column names come from the test, not input)."""
    columns = ", ".join(f":{name}" for name in row)
    conn.execute(text(f"INSERT INTO audit_events VALUES ({columns})"), row)  # noqa: S608


def rehash_from(conn: Connection, seq: int) -> None:
    """Recompute the chain from `seq` onwards, as a careful attacker would."""
    stored = rows(conn)
    prev = GENESIS_HASH if seq == 1 else str(stored[seq - 2]["event_hash"])
    for row in stored[seq - 1 :]:
        row["prev_hash"] = prev
        row["event_hash"] = event_hash(row)
        conn.execute(
            text("UPDATE audit_events SET prev_hash = :p, event_hash = :h WHERE seq = :s"),
            {"p": row["prev_hash"], "h": row["event_hash"], "s": row["seq"]},
        )
        prev = str(row["event_hash"])


# ------------------------------------------------------------ chain basics


def test_events_form_a_verifiable_chain(engine: Engine) -> None:
    write_events(engine, 3)
    with engine.connect() as conn:
        stored = rows(conn)
        result = verify(conn)
    assert [r["seq"] for r in stored] == [1, 2, 3]
    assert stored[0]["prev_hash"] == GENESIS_HASH
    assert stored[1]["prev_hash"] == stored[0]["event_hash"]
    assert result.ok and result.events == 3 and result.head_seq == 3


def test_empty_log_verifies(engine: Engine) -> None:
    with engine.connect() as conn:
        result = verify(conn)
    assert result.ok and result.events == 0


def test_denied_attempts_are_recorded_separately(engine: Engine) -> None:
    log = AuditLog(Redactor())
    with engine.begin() as conn:
        log.append(conn, AuditEvent("tool.call", "succeeded", actor_sub="sub-bob"))
        log.append(
            conn,
            AuditEvent(
                "tool.call",
                "denied",
                actor_sub="sub-alice",
                details={"reason": "source not in agent policy"},
            ),
        )
    with engine.connect() as conn:
        outcomes = dict(
            conn.execute(select(audit_events.c.actor_sub, audit_events.c.outcome)).all()
        )
    assert outcomes == {"sub-bob": "succeeded", "sub-alice": "denied"}


def test_event_is_rolled_back_with_its_action(engine: Engine) -> None:
    with pytest.raises(RuntimeError), engine.begin() as conn:
        AuditLog(Redactor()).append(conn, AuditEvent("task.submitted", "succeeded"))
        raise RuntimeError("the action failed")
    with engine.connect() as conn:
        assert rows(conn) == []


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"event_type": "Tool Call", "outcome": "succeeded"}, ValueError),
        ({"event_type": "toolcall", "outcome": "succeeded"}, ValueError),
        ({"event_type": "tool.call", "outcome": "allowed-ish"}, ValueError),
        ({"event_type": "tool.call", "outcome": "succeeded", "details": ["x"]}, TypeError),
    ],
)
def test_invalid_events_are_rejected(kwargs: dict[str, object], error: type[Exception]) -> None:
    with pytest.raises(error):
        AuditEvent(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------- database guards


def test_guard_triggers_exist_at_head(engine: Engine) -> None:
    with engine.connect() as conn:
        names = set(
            conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'trigger'")).scalars()
        )
    assert set(TRIGGERS) <= names


def test_update_is_rejected(engine: Engine) -> None:
    write_events(engine, 1)
    with pytest.raises(IntegrityError, match="append-only"), engine.begin() as conn:
        conn.execute(text("UPDATE audit_events SET outcome = 'denied'"))


def test_delete_is_rejected(engine: Engine) -> None:
    write_events(engine, 1)
    with pytest.raises(IntegrityError, match="append-only"), engine.begin() as conn:
        conn.execute(text("DELETE FROM audit_events"))


@pytest.mark.parametrize(
    "change", [{"prev_hash": "f" * 64}, {"seq": 5}], ids=["wrong-link", "skipped-seq"]
)
def test_insert_must_extend_the_head(engine: Engine, change: dict[str, object]) -> None:
    write_events(engine, 1)
    with engine.connect() as conn:
        forged = rows(conn)[0] | {"seq": 2, "event_id": "f" * 32, "event_hash": "e" * 64} | change
    with pytest.raises(IntegrityError, match="extend the chain head"), engine.begin() as conn:
        insert_raw(conn, forged)


# ---------------------------------------------------------------- redaction


def test_secrets_never_reach_stored_events(tmp_path: Path, engine: Engine) -> None:
    log = AuditLog(Redactor([CONFIGURED]))
    with engine.begin() as conn:
        appended = log.append(
            conn,
            AuditEvent(
                "gateway.call",
                "failed",
                actor_sub="sub-bob",
                correlation_id=JWT,
                details={"error": f"Authorization: Bearer {JWT}", "secret": CONFIGURED},
            ),
        )
    # The JWT pattern removes the token before the bearer pattern runs.
    assert appended.redacted == {"jwt", "configured-secret"}
    with engine.connect() as conn:
        stored = rows(conn)[0]
        assert verify(conn).ok
    assert JWT not in json.dumps(stored) and CONFIGURED not in json.dumps(stored)
    assert json.loads(str(stored["details_json"]))["redactions"] == sorted(appended.redacted)
    engine.dispose()
    for path in tmp_path.iterdir():  # the database file and its WAL
        content = path.read_bytes()
        assert JWT.encode() not in content and CONFIGURED.encode() not in content, path.name


# --------------------------------------------------------- tamper detection


def test_edited_event_is_detected(engine: Engine) -> None:
    write_events(engine, 3)
    with engine.begin() as conn:
        as_admin(conn)
        conn.execute(text("UPDATE audit_events SET outcome = 'denied' WHERE seq = 2"))
    with engine.connect() as conn:
        assert verify(conn).problems == ["seq 2: content does not match its hash"]


def test_edited_event_with_recomputed_hash_breaks_the_next_link(engine: Engine) -> None:
    write_events(engine, 3)
    with engine.begin() as conn:
        as_admin(conn)
        conn.execute(text("UPDATE audit_events SET outcome = 'denied' WHERE seq = 2"))
        row = rows(conn)[1]
        conn.execute(
            text("UPDATE audit_events SET event_hash = :h WHERE seq = 2"), {"h": event_hash(row)}
        )
    with engine.connect() as conn:
        assert verify(conn).problems == ["seq 3: does not link to the previous event"]


def test_deleted_event_is_detected(engine: Engine) -> None:
    write_events(engine, 3)
    with engine.begin() as conn:
        as_admin(conn)
        conn.execute(text("DELETE FROM audit_events WHERE seq = 2"))
    with engine.connect() as conn:
        problems = verify(conn).problems
    assert "seq 3: expected seq 2 (events missing or out of order)" in problems
    assert "seq 3: does not link to the previous event" in problems


def test_reordered_events_are_detected(engine: Engine) -> None:
    write_events(engine, 3)
    with engine.begin() as conn:
        as_admin(conn)
        conn.execute(text("UPDATE audit_events SET seq = 99 WHERE seq = 2"))
        conn.execute(text("UPDATE audit_events SET seq = 2 WHERE seq = 3"))
        conn.execute(text("UPDATE audit_events SET seq = 3 WHERE seq = 99"))
    with engine.connect() as conn:
        result = verify(conn)
    assert not result.ok
    assert any("seq 2" in p for p in result.problems)


def test_inserted_event_is_detected(engine: Engine) -> None:
    write_events(engine, 3)
    with engine.begin() as conn:
        as_admin(conn)
        conn.execute(text("UPDATE audit_events SET seq = seq + 100 WHERE seq >= 2"))
        conn.execute(text("UPDATE audit_events SET seq = seq - 99 WHERE seq >= 100"))
        forged = rows(conn)[0] | {"seq": 2, "event_id": "f" * 32, "outcome": "denied"}
        forged["prev_hash"] = rows(conn)[0]["event_hash"]
        forged["event_hash"] = event_hash(forged)
        insert_raw(conn, forged)
    with engine.connect() as conn:
        problems = verify(conn).problems
    assert "seq 3: content does not match its hash" in problems


def test_truncated_tail_is_detected_only_by_an_anchor(tmp_path: Path, engine: Engine) -> None:
    write_events(engine, 3)
    anchor_file = tmp_path / "anchors.jsonl"
    with engine.connect() as conn:
        write_anchor(conn, anchor_file)
    with engine.begin() as conn:
        as_admin(conn)
        conn.execute(text("DELETE FROM audit_events WHERE seq = 3"))
    with engine.connect() as conn:
        without = verify(conn)
        with_anchor = verify(conn, read_anchors(anchor_file))
    assert without.ok  # the honest limitation: a clean cut is invisible without anchors
    assert with_anchor.problems == [
        f"anchor seq 3 ({read_anchors(anchor_file)[0].anchored_at}): event missing (log truncated?)"
    ]


def test_fully_recomputed_history_is_detected_only_by_an_anchor(
    tmp_path: Path, engine: Engine
) -> None:
    write_events(engine, 3)
    anchor_file = tmp_path / "anchors.jsonl"
    with engine.connect() as conn:
        write_anchor(conn, anchor_file)
    with engine.begin() as conn:
        as_admin(conn)
        conn.execute(text("UPDATE audit_events SET outcome = 'denied' WHERE seq = 2"))
        rehash_from(conn, 2)
    with engine.connect() as conn:
        assert verify(conn).ok
        problems = verify(conn, read_anchors(anchor_file)).problems
    assert len(problems) == 1 and "hash differs (history rewritten?)" in problems[0]


# ------------------------------------------------------------------ anchors


def test_anchor_file_is_owner_only_json_lines(tmp_path: Path, engine: Engine) -> None:
    write_events(engine, 2)
    anchor_file = tmp_path / "anchors.jsonl"
    with engine.connect() as conn:
        first = write_anchor(conn, anchor_file)
    write_events(engine, 1)
    with engine.connect() as conn:
        second = write_anchor(conn, anchor_file)
        result = verify(conn, read_anchors(anchor_file))
    assert [a.seq for a in read_anchors(anchor_file)] == [first.seq, second.seq] == [2, 3]
    assert stat.S_IMODE(anchor_file.stat().st_mode) == 0o600
    assert result.ok and result.anchors_checked == 2 and result.unanchored_tail == 0


def test_unanchored_tail_is_reported(tmp_path: Path, engine: Engine) -> None:
    write_events(engine, 2)
    with engine.connect() as conn:
        write_anchor(conn, tmp_path / "anchors.jsonl")
    write_events(engine, 3)
    with engine.connect() as conn:
        assert verify(conn, read_anchors(tmp_path / "anchors.jsonl")).unanchored_tail == 3


def test_empty_log_cannot_be_anchored(tmp_path: Path, engine: Engine) -> None:
    with engine.connect() as conn, pytest.raises(ValueError, match="empty"):
        write_anchor(conn, tmp_path / "anchors.jsonl")
    assert read_anchors(tmp_path / "anchors.jsonl") == []
